#!/usr/bin/env python3
"""
PTT Stock 版 置頂盤中/盤後討論串 彈幕 (WebSocket 即時版)

核心原則：
- 完全透過 wss://ws.ptt.cc/bbs WebSocket 取得即時推文
- 導航只依「當下終端畫面」決定動作，不靠長期 flag 記狀態
- 推/噓/→ 白字彈幕、深黑半透明、多軌道
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import queue
import random
import re
import socket
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests
import websocket
from websocket import WebSocketConnectionClosedException

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QRectF, QPoint, QSize
from PyQt6.QtGui import (
    QPainter, QFont, QColor, QPen, QAction, QPixmap, QFontMetrics, QImageReader,
)
from PyQt6.QtWidgets import QApplication, QWidget, QMenu

from ptt_common import (
    encode_big5,
    encode_login_field,
    env_kick_other_sessions,
    load_ptt_credentials,
    try_decode,
)

# ===================== PTT constants =====================

PTT_WS_URL = "wss://ws.ptt.cc/bbs"
ORIGIN = "https://term.ptt.cc"

PTT_KEY_LEFT = "\x1b[D"
PTT_KEY_RIGHT = "\x1b[C"
PTT_KEY_UP = "\x1b[A"
PTT_KEY_DOWN = "\x1b[B"
PTT_KEY_HOME = "\x1b[1~"
# 文章內「跳到文末」用 Page End（不是看板列表的 $）
PTT_KEY_END = "\x1b[4~"
PTT_KEY_END_ALT = "\x1b[F"  # 部分終端 / xterm 的 End
PTT_KEY_PAGEUP = "\x1b[5~"
PTT_KEY_PAGEDOWN = "\x1b[6~"

# 嚴格推文列（行首 + 合法 PTT id + 冒號）。禁止行中 soft match，避免假推文/時間戳進彈幕。
PUSH_LINE_STRICT_RE = re.compile(
    r"^(推|噓|→)\s+([A-Za-z0-9_]{2,30})\s*[:：]\s*(.*)$"
)
# 尾端 metadata：IP / MM/DD HH:MM；時間後可能黏螢幕殘字（如 22:43tock）
PUSH_TRAIL_TIME_RE = re.compile(
    r"\s+\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\S*"
)
PUSH_TRAIL_IP_RE = re.compile(
    r"\s+\d{1,3}(?:\.\d{1,3}){3}\S*"
)
# 整段內容若只是時間/IP 則丟棄
PUSH_ONLY_META_RE = re.compile(
    r"^(?:"
    r"\d{1,2}/\d{1,2}(?:\s+\d{1,2}:\d{2}\S*)?"  # 07/18 或 07/18 14:30tock
    r"|\d{1,2}:\d{2}\S*"  # 14:30
    r"|\d{1,3}(?:\.\d{1,3}){3}\S*"  # IP
    r")$"
)
# 舊名保留給測試/相容
PUSH_TAG_RE = re.compile(r"^(推|噓|→)\s")
PUSH_LINE_RE = PUSH_LINE_STRICT_RE
# 盤中 / 盤後討論串標題（置底常見）
THREAD_TITLE_RE = re.compile(
    r"(?:\[閒聊\]\s*)?(?P<date>\d{4}/\d{2}/\d{2})?\s*(?P<kind>盤中閒聊|盤後閒聊|盤中|盤後)"
)
# PttChrome parseStatusRow：文章閱讀狀態列（最底列）
# 例：  瀏覽 第 1/20 頁 (  5%)  目前顯示: 第 01~22 行  (y)回應 (X%)推文 (h)說明   (←)離開
STATUS_ROW_RE = re.compile(
    r"瀏覽\s*第\s*(\d{1,3})(?:/(\d{1,3}))?\s*頁\s*\(\s*(\d{1,3})%\)\s*目前顯示:\s*第\s*0*(\d+)~0*(\d+)\s*行"
)


# ===================== helpers =====================

def _log_safe(text: str, max_len: int = 160) -> str:
    if not text:
        return ""
    text = text.replace("\x1b", "^[").replace("\r", "").replace("\n", " ")
    text = text.replace("\ufffd", "?")
    text = "".join(c if (c.isprintable() or c in " \t") else "?" for c in text)
    return text[:max_len]


# ===================== Terminal screen buffer =====================

class TerminalBuffer:
    """簡易 ANSI 終端緩衝：只保留「當下畫面」，不累積歷史文字。"""

    def __init__(self, rows: int = 24, cols: int = 80):
        self.rows = rows
        self.cols = cols
        self.buf: List[List[str]] = [[" "] * cols for _ in range(rows)]
        self.r = 0
        self.c = 0
        # 跨 feed 的未完成 ESC/CSI（WS 常把序列切半）
        self._esc_pending = ""

    def clear(self):
        self.buf = [[" "] * self.cols for _ in range(self.rows)]
        self.r = 0
        self.c = 0

    def snapshot(self) -> str:
        lines = []
        for row in self.buf:
            lines.append("".join(row).rstrip())
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    def lines(self) -> List[str]:
        return ["".join(row).rstrip() for row in self.buf]

    def _put(self, ch: str):
        if ch == "\n":
            self.r = min(self.rows - 1, self.r + 1)
            self.c = 0
            return
        if ch == "\r":
            self.c = 0
            return
        if ch == "\b":
            self.c = max(0, self.c - 1)
            return
        if ch == "\x0f" or ch == "\x0e" or ch == "\x00":
            return
        if ord(ch) < 32 and ch not in ("\t",):
            return
        if ch == "\t":
            self.c = min(self.cols - 1, (self.c // 8 + 1) * 8)
            return
        if self.c >= self.cols:
            self.c = 0
            self.r = min(self.rows - 1, self.r + 1)
        if 0 <= self.r < self.rows and 0 <= self.c < self.cols:
            self.buf[self.r][self.c] = ch
            self.c += 1

    def feed(self, text: str):
        if self._esc_pending:
            text = self._esc_pending + text
            self._esc_pending = ""
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b":
                i = self._feed_esc(text, i)
                continue
            self._put(ch)
            i += 1

    def _feed_esc(self, text: str, i: int) -> int:
        """解析 ESC 序列，回傳下一個未消費 index。未完成則寫入 _esc_pending。"""
        n = len(text)
        if i + 1 >= n:
            # 只有 ESC，等下一包
            self._esc_pending = text[i:]
            if len(self._esc_pending) > 64:
                self._esc_pending = ""
            return n
        if text[i + 1] != "[":
            return i + 2

        j = i + 2
        while j < n and not ("A" <= text[j] <= "Z" or "a" <= text[j] <= "z"):
            j += 1
        if j >= n:
            # CSI 未完成
            self._esc_pending = text[i:]
            if len(self._esc_pending) > 96:
                self._esc_pending = ""
            return n

        params_str = text[i + 2 : j]
        cmd = text[j]
        params = []
        if params_str:
            for part in params_str.split(";"):
                if part.isdigit():
                    params.append(int(part))
                elif part == "":
                    params.append(0)
                else:
                    params = []
                    break

        def p(idx: int, default: int = 0) -> int:
            return params[idx] if idx < len(params) else default

        if cmd == "H" or cmd == "f":
            row = max(1, p(0, 1)) - 1
            col = max(1, p(1, 1)) - 1
            self.r = min(self.rows - 1, row)
            self.c = min(self.cols - 1, col)
        elif cmd == "A":
            self.r = max(0, self.r - max(1, p(0, 1)))
        elif cmd == "B":
            self.r = min(self.rows - 1, self.r + max(1, p(0, 1)))
        elif cmd == "C":
            self.c = min(self.cols - 1, self.c + max(1, p(0, 1)))
        elif cmd == "D":
            self.c = max(0, self.c - max(1, p(0, 1)))
        elif cmd == "J":
            mode = p(0, 0)
            if mode == 2 or mode == 3:
                self.clear()
            elif mode == 0:
                for c in range(self.c, self.cols):
                    self.buf[self.r][c] = " "
                for r in range(self.r + 1, self.rows):
                    self.buf[r] = [" "] * self.cols
            elif mode == 1:
                for r in range(0, self.r):
                    self.buf[r] = [" "] * self.cols
                for c in range(0, self.c + 1):
                    self.buf[self.r][c] = " "
        elif cmd == "K":
            mode = p(0, 0)
            if mode == 0:
                for c in range(self.c, self.cols):
                    self.buf[self.r][c] = " "
            elif mode == 1:
                for c in range(0, self.c + 1):
                    self.buf[self.r][c] = " "
            elif mode == 2:
                self.buf[self.r] = [" "] * self.cols
        elif cmd == "m":
            pass
        return j + 1


# ===================== Screen detection (current frame only) =====================

class Screen(Enum):
    LOGIN_ID = auto()
    LOGIN_PASSWORD = auto()
    DUPLICATE_LOGIN = auto()
    ANYKEY = auto()
    MAIN_MENU = auto()
    BOARD_NAME_PROMPT = auto()
    BOARD_LIST = auto()
    TITLE_SEARCH_PROMPT = auto()
    ARTICLE = auto()
    PMORE_HELP = auto()
    LOGGING_IN = auto()
    UNKNOWN = auto()


def _last_nonempty_lines(frame: str, n: int = 3) -> List[str]:
    lines = [ln for ln in frame.splitlines() if ln.strip()]
    return lines[-n:] if lines else []


def is_pmore_help(frame: str) -> bool:
    """文章內按 h 跳出的 pmore 使用說明（需 LEFT/q 關掉，否則卡死）。"""
    if not frame:
        return False
    low = frame.lower()
    if "pmore" in low and ("使用說明" in frame or "more:" in low or "瀏覽程式" in frame):
        return True
    if "瀏覽程式使用說明" in frame:
        return True
    if "基本移動" in frame and "進階瀏覽" in frame and ("下翻一頁" in frame or "搜尋關鍵字" in frame):
        return True
    return False


def parse_status_row(frame: str) -> Optional[dict]:
    """PttChrome 式：用最底列「瀏覽 第…頁…目前顯示」判斷在閱讀文章。"""
    for line in reversed(frame.splitlines()):
        s = line.strip()
        if not s:
            continue
        m = STATUS_ROW_RE.search(s)
        if m:
            total = m.group(2)
            return {
                "page_index": int(m.group(1)),
                "page_total": int(total) if total else None,
                "page_percent": int(m.group(3)),
                "row_start": int(m.group(4)),
                "row_end": int(m.group(5)),
                "raw": s,
            }
        # 寬鬆備援
        if "瀏覽" in s and "目前顯示" in s and ("離開" in s or "←" in s):
            pct = re.search(r"\(\s*(\d{1,3})%\)", s)
            return {
                "page_index": 0,
                "page_total": None,
                "page_percent": int(pct.group(1)) if pct else 0,
                "row_start": 0,
                "row_end": 0,
                "raw": s,
            }
    return None


def extract_article_title(frame: str) -> str:
    """從文章畫面抓標題列文字。"""
    for line in frame.splitlines():
        s = line.strip()
        # 常見：標題  [閒聊] 2026/07/17 盤後閒聊
        m = re.match(r"標題\s*(.+)$", s)
        if m:
            return m.group(1).strip()
        m2 = re.search(r"標題\s*[:：]?\s*(.+)$", s)
        if m2 and "看板" not in s[:6]:
            return m2.group(1).strip()
    return ""


def is_target_stock_thread(frame: str) -> bool:
    """進文後檢查：是否為盤中/盤後閒聊目標串（anchor 飄移時會進錯文）。

    只認閒聊串，避免新聞標題「盤中殺尾盤」等誤判。
    """
    title = extract_article_title(frame)
    blob = title if title else "\n".join(frame.splitlines()[:8])
    # 標準： [閒聊] YYYY/MM/DD 盤中/盤後閒聊
    if re.search(r"\[閒聊\].*盤[中後]", blob):
        return True
    if re.search(r"\d{4}/\d{2}/\d{2}\s*盤[中後](?:閒聊)?", blob):
        return True
    if re.search(r"盤[中後]閒聊", blob):
        return True
    return False


def keyword_for_thread(best: dict) -> str:
    """由列表候選列組成標題搜尋關鍵字。"""
    if best.get("date") and len(best["date"]) >= 8:
        return f"{best['date']} {'盤中' if best.get('kind') == '盤中' else '盤後'}"
    return "盤中" if best.get("kind") == "盤中" else "盤後閒聊"


def has_search_hit_cursor(frame: str) -> bool:
    """搜尋結果列上是否有游標且像閒聊/日期目標（避免盲 RIGHT 進公告）。"""
    for line in frame.splitlines():
        s = line.strip()
        if "●" not in s and not s.lstrip().startswith(">"):
            continue
        if "[閒聊]" in s or "盤中" in s or "盤後" in s:
            return True
        # 搜尋結果常截成「[閒聊] 2026/」
        if re.search(r"\d{4}/\d{0,2}", s) and ("閒聊" in s or "盤" in s):
            return True
    return False


def detect_screen(frame: str) -> Screen:
    """只看當下畫面文字，不看歷史、不看 flag。"""
    if not frame or not frame.strip():
        return Screen.UNKNOWN

    # 登入過程中間狀態
    if any(x in frame for x in ("正在檢查帳號與密碼", "密碼正確", "開始登入系統", "登入中，請稍候", "正在更新與同步線上使用者")):
        return Screen.LOGGING_IN

    if "請輸入您的密碼" in frame or ("請輸入密碼" in frame and "代號" not in frame.split("請輸入密碼")[0][-20:]):
        if "請輸入您的密碼" in frame or re.search(r"請輸入.*密碼", frame):
            return Screen.LOGIN_PASSWORD

    if any(x in frame for x in ("請輸入代號", "請輸入您的代號", "或以 guest 參觀", "代號，或以 guest")):
        return Screen.LOGIN_ID

    if "刪除其他重複登入" in frame or ("其它連線" in frame and "[Y/n]" in frame) or (
        "重複登入" in frame and "[Y/n]" in frame
    ):
        return Screen.DUPLICATE_LOGIN

    # pmore 說明（優先於 ARTICLE：殘留「瀏覽」字樣勿當文內）
    if is_pmore_help(frame):
        return Screen.PMORE_HELP

    # 任意鍵 cover（優先看最底列，貼近 PttChrome pageState=5）
    bottom = "\n".join(_last_nonempty_lines(frame, 4))
    if "請按任意鍵繼續" in bottom or "按任意鍵繼續" in bottom or "請按 空白鍵 繼續" in bottom:
        return Screen.ANYKEY
    if "請按任意鍵繼續" in frame or "按任意鍵繼續" in frame:
        return Screen.ANYKEY
    if "本日十大熱門話題" in frame or "十大熱門話題" in frame:
        return Screen.ANYKEY
    if "請勿頻繁登入" in frame:
        return Screen.ANYKEY

    # 看板列表（優先於標題搜尋：搜尋結果仍是列表 UI，殘留「搜尋標題」字樣勿誤判）
    if (
        "看板《Stock》" in frame
        or "看板《Sto" in frame  # 截斷
        or "系列《Stock》" in frame
        or ("[股票]" in frame and "文章選讀" in frame)
    ):
        return Screen.BOARD_LIST
    if "文章列表" in frame and "Stock" in frame:
        return Screen.BOARD_LIST
    if "文章選讀" in frame and ("編號" in frame or "人氣" in frame or "日 期" in frame):
        return Screen.BOARD_LIST

    # 標題搜尋輸入：只看底部幾行，且不能已是列表
    bottom = "\n".join(_last_nonempty_lines(frame, 6))
    if any(
        x in bottom
        for x in (
            "請輸入想要搜尋的標題",
            "請輸入欲搜尋的標題",
            "請輸入標題關鍵字",
        )
    ):
        return Screen.TITLE_SEARCH_PROMPT

    # 文章內：PttChrome 以狀態列為準（pageState=3）
    if parse_status_row(frame):
        return Screen.ARTICLE

    if ("作者" in frame and "標題" in frame and "時間" in frame) or "看板: Stock" in frame or "看板:Stock" in frame:
        if "文章列表" not in frame and "看板《" not in frame:
            return Screen.ARTICLE
        if re.search(r"作者\s*[^\n]+", frame) and re.search(r"標題\s*[^\n]+", frame):
            if "編號" not in frame or "日 期" not in frame:
                return Screen.ARTICLE

    # 輸入看板名稱
    if any(
        x in frame
        for x in (
            "請輸入看板名稱",
            "請輸入欲進入的看板",
            "【 選擇看板 】",
            "按空白鍵自動搜尋",
        )
    ):
        return Screen.BOARD_NAME_PROMPT

    # 主功能表（嚴格：避免看板/歡迎畫面誤判）
    if "【主功能表】" in frame:
        return Screen.MAIN_MENU
    if "主功能表" in frame and "分組討論區" in frame and "私人信件區" in frame:
        return Screen.MAIN_MENU

    return Screen.UNKNOWN


def find_target_threads(frame: str) -> List[dict]:
    """從當下畫面找出盤中/盤後（或搜尋結果裡的 [閒聊] 日更串）。"""
    found = []
    for line in frame.splitlines():
        s = line.strip()
        if not s:
            continue
        # 排除不相干
        if "搜尋" in s and "盤" not in s and "閒聊" not in s:
            continue

        kind = None
        date = None
        m = THREAD_TITLE_RE.search(s)
        if m:
            kind = m.group("kind")
            date = m.group("date")
            if kind and kind.startswith("盤中"):
                kind = "盤中"
            else:
                kind = "盤後"
        elif "盤中" in s or "盤後" in s:
            kind = "盤中" if "盤中" in s else "盤後"
            dm = re.search(r"(\d{4}/\d{2}/\d{2})", s)
            if dm:
                date = dm.group(1)
            else:
                dm2 = re.search(r"(\d{1,2}/\d{1,2})", s)
                date = dm2.group(1) if dm2 else None
        elif "[閒聊]" in s and re.search(r"\d{4}/\d{1,2}|\d{1,2}/\d{1,2}", s):
            # 搜尋結果常被截成「[閒聊] 2026/」沒有「盤後」二字
            kind = "盤後" if "盤中" not in s else "盤中"
            dm = re.search(r"(\d{4}/\d{2}/\d{2})", s)
            if dm:
                date = dm.group(1)
            else:
                dm2 = re.search(r"(\d{1,2}/\d{1,2})", s)
                date = dm2.group(1) if dm2 else None
        else:
            continue

        num = None
        nm = re.search(r"(\d{4,})", s)
        if nm:
            num = int(nm.group(1))

        starred = "★" in s or "＊" in s
        cursor = "●" in s or s.lstrip().startswith(">")
        found.append(
            {
                "line": s,
                "kind": kind,
                "date": date or "",
                "num": num or 0,
                "starred": starred,
                "cursor": cursor,
            }
        )
    return found


def pick_best_thread(candidates: List[dict]) -> Optional[dict]:
    """優先較新日期；同日優先盤中。"""
    if not candidates:
        return None

    def sort_key(c):
        # date 字串 YYYY/MM/DD 可直接比；MM/DD 次之
        d = c.get("date") or ""
        kind_rank = 0 if c.get("kind") == "盤中" else 1
        return (d, -c.get("num", 0), kind_rank)

    # 有完整日期的優先
    with_date = [c for c in candidates if c.get("date")]
    pool = with_date if with_date else candidates
    return sorted(pool, key=sort_key, reverse=True)[0]


# ===================== Push model =====================

@dataclass
class Push:
    tag: str
    user: str
    content: str
    raw: str


# ===================== WebSocket client (screen-driven) =====================

class PTTWebSocketClient(QObject):
    """
    背景 thread 跑 WebSocket。
    導航策略：feed 進 TerminalBuffer → detect_screen(snapshot) → 依畫面送一個動作。
    不用長期 flag 記「我現在在哪」；畫面本身就是狀態。
    """

    new_push = pyqtSignal(object)
    status = pyqtSignal(str)
    # UI：connecting | navigating | live | error | reconnecting
    ui_state = pyqtSignal(str)
    # 目前追蹤的文章標題（給彈幕 header）
    article_title = pyqtSignal(str)

    def __init__(
        self,
        account: Optional[str] = None,
        password: Optional[str] = None,
        action_cooldown: float = 0.85,
    ):
        super().__init__()
        if account and password:
            self.account = account
            self.password = password
        else:
            self.account, self.password = load_ptt_credentials()

        self.ws: Optional[websocket.WebSocket] = None
        self.thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.term = TerminalBuffer(24, 80)
        # 有序去重（FIFO 淘汰），避免 set 切片亂序
        self._seen_pushes: "OrderedDict[str, None]" = OrderedDict()
        self.action_cooldown = action_cooldown
        self._kick_other = env_kick_other_sessions()

        # 僅作「同一動作不要連打」的短冷卻（不是業務狀態 flag）
        self._last_action_key: Optional[str] = None
        self._last_action_time: float = 0.0
        self._last_screen: Screen = Screen.UNKNOWN
        self._last_space_time: float = 0.0
        # 進正確目標文後，待滿 interval 才 LEFT 刷新（減輕 PTT 負擔：5–10 秒隨機）
        self._article_entered_at: float = 0.0
        self._refresh_interval: float = self._next_refresh_interval()
        # 列表重找：避免 board_end ↔ title_jump 盤後/盤中 無限來回
        self._search_tried_intraday: bool = False
        self._search_backoff_until: float = 0.0
        self._search_cycle_id: int = 0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self, join_timeout: float = 2.5):
        """停止 WS thread；可中斷重連 sleep，並盡量 join 掉背景執行緒。"""
        self._stop.set()
        ws = self.ws
        self.ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        th = self.thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=join_timeout)
            if th.is_alive():
                print("[INFO] WebSocket thread 尚未結束（將隨行程退出）")

    def _sleep_interruptible(self, seconds: float) -> bool:
        """可被 stop() 中斷的 sleep；回 True 表示被要求停止。"""
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if self._stop.is_set():
                return True
            time.sleep(min(0.2, end - time.time()))
        return self._stop.is_set()

    @staticmethod
    def _next_refresh_interval() -> float:
        """Live 刷新間隔：5–10 秒均勻隨機，避免固定節奏壓站。"""
        return random.uniform(5.0, 10.0)

    # ---------- IO ----------

    def _send_bytes(self, data: bytes):
        if not self.ws:
            return
        try:
            self.ws.send(data, websocket.ABNF.OPCODE_BINARY)
        except Exception as e:
            self.status.emit(f"送出失敗: {e}")

    def _send_text(self, s: str):
        # ASCII / control 直接 latin-1；中文請走 _send_big5
        self._send_bytes(s.encode("latin-1", errors="replace"))

    def _send_login_field(self, text: str, label: str) -> bool:
        b, err = encode_login_field(text)
        if err or b is None:
            self.status.emit(f"{label}無法編碼傳送：{err or 'unknown'}")
            print(f"[INFO] {label} encode fail: {err}")
            return False
        self._send_bytes(b + b"\r")
        return True

    def _send_big5(self, s: str):
        self._send_bytes(encode_big5(s))

    def _remember_push_key(self, key: str) -> bool:
        """True if new (not seen). FIFO cap."""
        if key in self._seen_pushes:
            # 移到最新
            self._seen_pushes.move_to_end(key)
            return False
        self._seen_pushes[key] = None
        while len(self._seen_pushes) > 2500:
            self._seen_pushes.popitem(last=False)
        return True

    def _act(self, key: str, payload=None, *, big5: bool = False) -> bool:
        """送出一個動作；相同 key 在 cooldown 內不重送（只防連打，不記業務狀態）。"""
        now = time.time()
        special_cd = {
            "login_id": 2.0,
            "login_id_guest": 2.0,
            "login_password": 3.0,
            "duplicate_y": 2.5,
            "duplicate_n": 2.5,
            "board_stock": 2.0,
            "title_jump": 2.2,
            "title_search": 1.6,
            "enter_article": 1.5,
            "board_end": 2.5,
            "article_end": 1.5,
            "article_wrong": 1.2,
            "refresh_leave": 2.5,
            "pmore_dismiss": 1.0,
            "anykey_space": 0.9,
            "main_s": 1.5,
            "search_backoff": 0.0,
        }
        cd = special_cd.get(key, self.action_cooldown)
        if key.startswith("title_jump"):
            # 不同關鍵字之間也共用較長冷卻，避免 盤後/盤中 連打
            if (self._last_action_key or "").startswith("title_jump") and (now - self._last_action_time) < 3.5:
                return False
            cd = 2.2
        if key == self._last_action_key and (now - self._last_action_time) < cd:
            return False

        if key in ("login_password", "login_id", "login_id_guest"):
            print(f"[ACTION] {key}")
        elif payload not in (None, "", " ", "s", "?", "$") and not str(payload).startswith("\x1b"):
            print(f"[ACTION] {key} ({_log_safe(str(payload), 40)})")
        else:
            print(f"[ACTION] {key}")
        if payload is not None:
            if big5 and isinstance(payload, str):
                self._send_big5(payload)
            elif isinstance(payload, bytes):
                self._send_bytes(payload)
            else:
                self._send_text(payload)

        self._last_action_key = key
        self._last_action_time = now
        return True

    def _title_jump(self, keyword: str) -> bool:
        """在看板列表開標題搜尋並一次送完關鍵字（不依賴業務 flag）。"""
        # action key 帶關鍵字，避免盤中/盤後互相卡 cooldown
        if not self._act(f"title_jump:{keyword}", "?"):
            return False
        if self._sleep_interruptible(0.4):
            return False
        self._send_big5(keyword + "\r")
        print(f"[ACTION] title_keyword ({_log_safe(keyword, 40)})")
        return True

    # ---------- main loop ----------

    def _emit_ui(self, state: str):
        try:
            self.ui_state.emit(state)
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            self.status.emit("正在連線 wss://ws.ptt.cc/bbs ...")
            self._emit_ui("connecting")
            self.term = TerminalBuffer(24, 80)
            self._last_action_key = None
            self._last_action_time = 0.0
            self._last_screen = Screen.UNKNOWN
            self._article_entered_at = 0.0
            self._search_tried_intraday = False
            self._search_backoff_until = 0.0
            self._search_cycle_id = 0
            last_error = ""
            ws = None

            try:
                ws = websocket.WebSocket()
                ws.connect(
                    PTT_WS_URL,
                    origin=ORIGIN,
                    header=[
                        "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                        "Accept-Language: zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                    ],
                    timeout=10,
                )
                self.ws = ws
                ws.settimeout(1.0)
                self.status.emit("WebSocket 已連線")
                self._emit_ui("navigating")

                while not self._stop.is_set():
                    try:
                        message = ws.recv()
                        if message is None:
                            break
                        if isinstance(message, bytes):
                            text = try_decode(message)
                        else:
                            text = str(message)

                        self.term.feed(text)
                        frame = self.term.snapshot()
                        screen = detect_screen(frame)

                        if screen != self._last_screen:
                            if screen != Screen.UNKNOWN:
                                print(f"[STATE] {screen.name}")
                            # 畫面切換時印摘要，方便確認是否真的在文章/列表
                            summary = " | ".join(
                                _log_safe(ln, 70) for ln in frame.splitlines() if ln.strip()
                            )[:240]
                            if summary:
                                print(f"[SCREEN] {summary}")
                            self._last_screen = screen

                        self._parse_pushes(frame, screen)
                        self._navigate(frame, screen)

                    except WebSocketConnectionClosedException:
                        break
                    except Exception as recv_err:
                        err_str = str(recv_err).lower()
                        if "timeout" in err_str or "timed out" in err_str:
                            # timeout 當 tick：用目前畫面再決策一次（keepalive / 卡住補送）
                            frame = self.term.snapshot()
                            screen = detect_screen(frame)
                            if screen != self._last_screen and screen != Screen.UNKNOWN:
                                print(f"[STATE] {screen.name}")
                                self._last_screen = screen
                            self._navigate(frame, screen)
                            continue
                        self.status.emit(f"接收錯誤: {str(recv_err)[:100]}")
                        break

            except WebSocketConnectionClosedException:
                pass
            except Exception as e:
                last_error = str(e)
                self.status.emit(f"WS Error: {last_error[:250]}")
                self._emit_ui("error")
            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self.ws = None

            if self._stop.is_set():
                break
            delay = 8 if "500" in last_error else 5
            self.status.emit(f"連線失敗，{delay} 秒後自動重試...")
            self._emit_ui("reconnecting")
            if self._sleep_interruptible(delay):
                break

        self.status.emit("WebSocket 結束")
        self._emit_ui("disconnected")

    # ---------- navigation: pure screen → action ----------

    def _navigate(self, frame: str, screen: Screen):
        if screen == Screen.LOGGING_IN or screen == Screen.UNKNOWN:
            return

        # 非文章畫面時視為導航中（給 UI 狀態點）
        if screen != Screen.ARTICLE and self._article_entered_at <= 0:
            if screen in (
                Screen.MAIN_MENU,
                Screen.BOARD_NAME_PROMPT,
                Screen.BOARD_LIST,
                Screen.TITLE_SEARCH_PROMPT,
                Screen.ANYKEY,
                Screen.LOGIN_ID,
                Screen.LOGIN_PASSWORD,
            ):
                self._emit_ui("navigating")

        if screen == Screen.LOGIN_ID:
            if self.account:
                if self._act("login_id", None):
                    if not self._send_login_field(self.account, "帳號"):
                        pass
            else:
                self._act("login_id_guest", "guest\r")
            return

        if screen == Screen.LOGIN_PASSWORD:
            if self._act("login_password", None):
                self._send_login_field(self.password or "", "密碼")
            return

        if screen == Screen.DUPLICATE_LOGIN:
            if self._kick_other:
                self._act("duplicate_y", "y\r")
            else:
                self._act("duplicate_n", "n\r")
            return

        if screen == Screen.ANYKEY:
            self._act("anykey_space", " ")
            return

        if screen == Screen.PMORE_HELP:
            # 關掉說明回到列表/文章（LEFT 與 PttChrome 離開一致）
            self._act("pmore_dismiss", PTT_KEY_LEFT)
            return

        if screen == Screen.MAIN_MENU:
            # 主功能表 → s 搜尋看板
            self._act("main_s", "s")
            return

        if screen == Screen.BOARD_NAME_PROMPT:
            self._act("board_stock", "Stock\r")
            return

        if screen == Screen.TITLE_SEARCH_PROMPT:
            # _title_jump 已送過 ?+關鍵字時不要再送一次（會弄亂結果）
            last = self._last_action_key or ""
            age = time.time() - self._last_action_time
            if last.startswith("title_jump") and age < 6.0:
                return
            keyword = self._search_keyword_from_context(frame)
            self._act("title_search", keyword + "\r", big5=True)
            return

        if screen == Screen.BOARD_LIST:
            self._nav_board_list(frame)
            return

        if screen == Screen.ARTICLE:
            self._nav_article(frame)
            return

    def _search_keyword_from_context(self, frame: str) -> str:
        """只依當下畫面線索選標題搜尋關鍵字。"""
        cands = find_target_threads(frame)
        best = pick_best_thread(cands)
        if best:
            return keyword_for_thread(best)
        # 預設先找盤後閒聊（收盤後較常需要）；盤中關鍵字較短易誤中
        return "盤後閒聊"

    def _begin_search_backoff(self, seconds: float = 8.0, reason: str = ""):
        """一整輪 盤後→盤中 都找不到時暫停，避免狂刷 PTT。"""
        self._search_backoff_until = time.time() + seconds
        self._search_tried_intraday = False
        self._search_cycle_id += 1
        if reason:
            print(f"[CHECK] 搜尋暫停 {seconds:.0f}s: {reason}")

    def _nav_board_list(self, frame: str):
        """
        列表導航。
        重點（對齊 PttChrome Live 文小幫手）：
        - 刷新時只會 LEFT 回列表；游標/anchor 可能已飄移
        - 因此「絕不能」假設游標仍在原文章，盲目 RIGHT
        - 必須依當下畫面確認目標列，再 > / Enter；進文後再驗標題
        - 錯文 LEFT 後：穩定 title 搜尋，禁止 board_end↔盤後↔盤中 無限翻轉
        """
        now = time.time()
        cands = find_target_threads(frame)
        best = pick_best_thread(cands)
        last = self._last_action_key or ""
        age = now - self._last_action_time

        just_refreshed = last == "refresh_leave" and age < 8.0
        just_wrong = last == "article_wrong" and age < 10.0
        just_searched = (last.startswith("title_jump") or last == "title_search") and age < 14.0
        just_end = last == "board_end" and age < 8.0
        just_enter = last == "enter_article" and age < 4.0

        # 找到目標就清 backoff / 盤中試過旗標
        if best:
            self._search_backoff_until = 0.0
            self._search_tried_intraday = False

        # 1) 游標已在目標列 → RIGHT
        if best and best.get("cursor"):
            self._act("enter_article", PTT_KEY_RIGHT)
            return

        # 2) 剛搜完：等結果；有目標或游標像搜尋命中才進
        if just_searched:
            if age < 2.0:
                return  # 等列表重繪
            if best:
                # 搜尋結果列出但 ● 偵測失敗 → 進第一筆目標
                self._act("enter_article", PTT_KEY_RIGHT)
                return
            if has_search_hit_cursor(frame):
                self._act("enter_article", PTT_KEY_RIGHT)
                return
            if age < 5.0:
                return  # 再等一會兒
            # 盤後找不到 → 試一次盤中；之後 backoff，不再 board_end 迴圈
            if "盤後" in last and not self._search_tried_intraday:
                self._search_tried_intraday = True
                self._title_jump("盤中")
                return
            if age < 10.0:
                return
            # 已在 backoff 中：只靜默等待，不要每 tick 重設計時
            if now < self._search_backoff_until:
                return
            self._begin_search_backoff(8.0, "盤後/盤中 皆無結果")
            return

        # 3) 畫面已有目標但游標不在其上 → 標題跳轉定位
        if best:
            keyword = keyword_for_thread(best)
            # 同一關鍵字短時間不重送
            if last == f"title_jump:{keyword}" and age < 6.0:
                if age >= 2.0:
                    self._act("enter_article", PTT_KEY_RIGHT)
                return
            if last.startswith("title_jump") and age < 4.0:
                return
            self._title_jump(keyword)
            return

        # 4) 畫面沒有目標（含 just_searched 已結束後仍在 backoff）
        if now < self._search_backoff_until:
            return

        # 剛 RIGHT 仍在列表（沒進文）→ 等一下，勿立刻 board_end
        if just_enter:
            return
        if last == "enter_article" and age < 8.0:
            # 進文失敗：直接重搜，不要 $ 再翻一次
            self._title_jump("盤後閒聊")
            return

        # 錯文 LEFT / Live 刷新 LEFT：等畫面穩後 title 搜尋（不必先 $）
        if just_wrong or just_refreshed:
            if age < 1.5:
                return
            self._search_tried_intraday = False
            self._title_jump(self._search_keyword_from_context(frame))
            return

        # 剛 $ 到板底：等列表更新
        if just_end:
            if age < 2.0:
                return
            # 板底仍無目標 → 搜盤後
            self._search_tried_intraday = False
            self._title_jump("盤後閒聊")
            return

        # 初始進板或迷路：先 $ 看置底串（只送一次，成功後走 just_end 分支）
        if last != "board_end" or age > 20.0:
            self._act("board_end", "$")
            return

        # last 仍是 board_end 且過了 just_end 窗：保險重搜
        self._title_jump("盤後閒聊")

    def _nav_article(self, frame: str):
        """
        文章內（對齊 PttChrome Live 文小幫手）：
        1) 進文後先檢查標題是不是盤中/盤後（> 後 anchor 可能進錯文）
        2) 錯文 → LEFT 回列表，由列表邏輯重找
        3) 對文 → Page End 跳文末載入推文
        4) 每隔 N 秒 LEFT 回列表刷新（完整刷新語意是 LEFT → 重定位 → RIGHT → END，
           但 RIGHT/定位必須依「當下列表畫面」做，不能一次盲送 LEFT+RIGHT+END）
        """
        now = time.time()
        last = self._last_action_key or ""
        has_push = bool(re.search(r"^[推噓→]", frame, re.M))
        status = parse_status_row(frame)

        # pmore 說明殘影（detect 漏網時）
        if is_pmore_help(frame):
            self._act("pmore_dismiss", PTT_KEY_LEFT)
            return

        # --- 進文檢查：有標題列才驗是不是盤中/盤後 ---
        # 文末常只剩狀態列 + 推文，沒有「標題」列，不能當錯文 LEFT（否則一直甩出）
        title = extract_article_title(frame)
        if title and not is_target_stock_thread(frame):
            print(f"[CHECK] 非目標文章，LEFT 重找: {_log_safe(title, 60)}")
            self._article_entered_at = 0.0
            self._search_tried_intraday = False
            self._act("article_wrong", PTT_KEY_LEFT)
            return

        if self._article_entered_at <= 0:
            self._article_entered_at = now
            self._search_backoff_until = 0.0
            self._search_tried_intraday = False
            self._emit_ui("live")
            if title:
                print(f"[CHECK] 目標文章確認: {_log_safe(title, 60)}")
                try:
                    self.article_title.emit(title)
                except Exception:
                    pass
            elif status:
                print("[CHECK] 文章狀態列確認（無標題列，視為仍在文內）")

        # --- 跳到文末（Page End = \x1b[4~，同 PttChrome）---
        at_end = False
        if status:
            if status.get("page_total") and status["page_index"] >= status["page_total"]:
                at_end = True
            elif status.get("page_percent", 0) >= 99:
                at_end = True

        need_end = (
            last in ("enter_article", "title_search")
            or last.startswith("title_jump")
            or (not at_end and not has_push and last not in ("article_end", "article_space"))
            or (not at_end and last == "enter_article")
        )
        if need_end and last != "article_end":
            if self._act("article_end", PTT_KEY_END):
                return

        # --- 定期 LEFT 回列表刷新（5–10s 隨機；回列表後重定位再進）---
        if now - self._article_entered_at >= self._refresh_interval:
            if self._act("refresh_leave", PTT_KEY_LEFT):
                self._article_entered_at = 0.0
                self._refresh_interval = self._next_refresh_interval()
                return

        # 文末輕量 keepalive（空白）；間隔略放長，減少多餘按鍵
        if not at_end and now - self._last_space_time > 10.0:
            if self._act("article_end", PTT_KEY_END):
                return

        if now - self._last_space_time > 6.0:
            self._send_text(" ")
            self._last_space_time = now
            self._last_action_key = "article_space"
            self._last_action_time = now

    # ---------- push parse ----------

    @staticmethod
    def _clean_push_content(content: str) -> str:
        """去掉尾端 IP / MM/DD HH:MM（含時間後螢幕殘字）；拒絕只剩時間戳。"""
        if not content:
            return ""
        content = content.replace("\x00", " ").strip()
        # 從「最後一個像推文時間的片段」切掉到行尾
        # 例：留言……  07/18 22:43tock  → 留言……
        m = None
        for m in PUSH_TRAIL_TIME_RE.finditer(content):
            pass
        if m:
            content = content[: m.start()].rstrip()
        m_ip = None
        for m_ip in PUSH_TRAIL_IP_RE.finditer(content):
            pass
        if m_ip:
            # 只剝「很靠行尾」的 IP（後面幾乎沒正文）
            if m_ip.end() >= len(content) - 2 or content[m_ip.end():].strip() == "":
                content = content[: m_ip.start()].rstrip()
        content = content.strip()
        if not content or PUSH_ONLY_META_RE.match(content):
            return ""
        # 再保險：仍以時間結尾
        content = re.sub(
            r"\s+\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\S*\s*$", "", content
        ).strip()
        if not content or PUSH_ONLY_META_RE.match(content):
            return ""
        # 控制字元 / 替換字
        content = "".join(c if c.isprintable() or c in " \t" else "" for c in content)
        content = content.replace("\ufffd", "").strip()
        # 壓縮多空白；不截斷正文（完整顯示）
        content = re.sub(r"\s{2,}", " ", content)
        return content

    def _parse_pushes(self, frame: str, screen: Screen):
        if screen != Screen.ARTICLE:
            return
        for raw_line in frame.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # UI / 狀態列
            if any(
                x in line
                for x in (
                    "文章選讀",
                    "目前顯示",
                    "瀏覽 第",
                    "請按任意鍵",
                    "看板《",
                    "作者",
                    "標題",
                )
            ) and not PUSH_LINE_STRICT_RE.match(line):
                continue

            # 只接受行首 推/噓/→ + 合法 id + 冒號（不再 soft match 行中）
            m = PUSH_LINE_STRICT_RE.match(line)
            if not m:
                continue
            tag, user, content = m.group(1), m.group(2), m.group(3)
            content = self._clean_push_content(content)
            if not content:
                continue
            # 使用者 id 再確認（英數底線）
            if not re.fullmatch(r"[A-Za-z0-9_]{2,30}", user):
                continue

            key = hashlib.sha1(f"{tag}|{user}|{content}".encode("utf-8")).hexdigest()
            if not self._remember_push_key(key):
                continue

            push = Push(tag=tag, user=user, content=content, raw=line)
            self.new_push.emit(push)


# ===================== Image cache (URL → disk → QPixmap) =====================

IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image")
IMG_PLACEHOLDER = "[圖]"
IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8MB
IMAGE_MAX_CONCURRENT = 3
# 圖片 URL：副檔名、/image/png 這類 path、常見無副檔名圖床
IMAGE_URL_LOOSE_RE = re.compile(
    r"https?://[^\s<>\"'\]\)]+?"
    r"(?:"
    r"\.(?:png|jpe?g|gif|webp|bmp)"
    r"|/(?:image|images|img)/(?:png|jpe?g|jpg|gif|webp)"
    r")"
    r"(?:[^\s<>\"'\]\)]*)?",
    re.IGNORECASE,
)
IMGUR_URL_RE = re.compile(
    r"https?://(?:i\.)?imgur\.com/[\w]+(?:\.(?:png|jpe?g|gif|webp))?",
    re.IGNORECASE,
)
EXTRA_IMAGE_HOST_RE = re.compile(
    r"https?://(?:(?:i\.)?meee\.com\.tw|pbs\.twimg\.com|media\.giphy\.com|i\.giphy\.com|"
    r"i\.ibb\.co|i\.postimg\.cc)[^\s<>\"'\]\)]+",
    re.IGNORECASE,
)


def _sanitize_image_url(url: str) -> str:
    return url.rstrip(".,;:!?)】」』\"'")


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def image_url_safe(url: str) -> bool:
    """只擋明顯危險目標（非 http(s)、解析到內網 IP）；不做 domain allowlist。"""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = p.hostname
    if not host:
        return False
    # hostname 本身是 IP 時直接檢查
    try:
        if not _is_public_ip(host):
            # host 可能是域名
            ipaddress.ip_address(host)
            return False
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            addr = info[4][0]
            if not _is_public_ip(addr):
                return False
    except Exception:
        # DNS 失敗：仍允許嘗試下載（requests 會再失敗）
        return True
    return True


def extract_image_urls_and_placeholder(content: str) -> Tuple[str, List[str]]:
    """把內容裡的圖片 URL 換成 [圖]，並回傳 urls（順序對應每個 [圖]）。"""
    urls: List[str] = []

    def repl(m: re.Match) -> str:
        u = _sanitize_image_url(m.group(0))
        if not u:
            return m.group(0)
        if not image_url_safe(u):
            # 內網等危險目標：保留原文，不下載
            return m.group(0)
        if u in urls:
            return IMG_PLACEHOLDER
        urls.append(u)
        return IMG_PLACEHOLDER

    out = IMAGE_URL_LOOSE_RE.sub(repl, content)
    out = IMGUR_URL_RE.sub(repl, out)
    out = EXTRA_IMAGE_HOST_RE.sub(repl, out)
    return out, urls


def cache_key_for_url(url: str) -> str:
    """以 URL 的 urlsafe base64 當快取鍵；過長則改 sha1（不重複下載）。"""
    raw = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    if len(raw) <= 160:
        return raw
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


@dataclass
class CachedImage:
    """靜態圖或 GIF 多幀；彈幕飛行期間依時間 loop 播。"""
    frames: List[QPixmap]
    delays_ms: List[int]
    total_ms: int = 0
    # (w,h) -> pre-scaled frames（避免每 paint 都 Smooth scale）
    _scaled: Dict[Tuple[int, int], List[QPixmap]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not self.delays_ms and self.frames:
            self.delays_ms = [100] * len(self.frames)
        if len(self.delays_ms) < len(self.frames):
            self.delays_ms.extend([100] * (len(self.frames) - len(self.delays_ms)))
        self.delays_ms = [max(20, d if d > 0 else 100) for d in self.delays_ms[: len(self.frames)]]
        self.total_ms = max(1, sum(self.delays_ms))
        if not hasattr(self, "_scaled") or self._scaled is None:
            self._scaled = {}

    @property
    def is_animated(self) -> bool:
        return len(self.frames) > 1

    def frame_at(self, t_sec: float) -> QPixmap:
        if not self.frames:
            return QPixmap()
        if len(self.frames) == 1:
            return self.frames[0]
        ms = int(t_sec * 1000.0) % self.total_ms
        acc = 0
        for pm, d in zip(self.frames, self.delays_ms):
            acc += d
            if ms < acc:
                return pm
        return self.frames[-1]

    def first(self) -> QPixmap:
        return self.frames[0] if self.frames else QPixmap()

    def scaled_frame_at(self, t_sec: float, size: QSize) -> QPixmap:
        key = (size.width(), size.height())
        if key not in self._scaled:
            self._scaled[key] = [
                f.scaled(
                    size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                for f in self.frames
            ]
            # 限制快取組數
            while len(self._scaled) > 4:
                self._scaled.pop(next(iter(self._scaled)))
        frames = self._scaled[key]
        if not frames:
            return QPixmap()
        if len(frames) == 1:
            return frames[0]
        ms = int(t_sec * 1000.0) % self.total_ms
        acc = 0
        for pm, d in zip(frames, self.delays_ms):
            acc += d
            if ms < acc:
                return pm
        return frames[-1]


def load_cached_image_from_path(path: str) -> Optional[CachedImage]:
    """用 QImageReader 載入；GIF 拆成多幀 + delay（loop 由 frame_at 負責）。"""
    if not path or not os.path.isfile(path):
        return None
    reader = QImageReader(path)
    if not reader.canRead():
        pm = QPixmap(path)
        if pm.isNull():
            return None
        return CachedImage(frames=[pm], delays_ms=[100])

    frames: List[QPixmap] = []
    delays: List[int] = []
    # Qt 上 jumpToNextImage 對部分 GIF 不可靠，改 imageCount + jumpToImage
    n = reader.imageCount()
    if n <= 0:
        n = 1
    n = min(n, 200)
    for i in range(n):
        if n > 1:
            reader.jumpToImage(i)
        img = reader.read()
        if img.isNull():
            break
        frames.append(QPixmap.fromImage(img))
        delays.append(int(reader.nextImageDelay()))

    if not frames:
        pm = QPixmap(path)
        if pm.isNull():
            return None
        return CachedImage(frames=[pm], delays_ms=[100])
    return CachedImage(frames=frames, delays_ms=delays)


class ImageCache(QObject):
    """下載圖片到 image/，記憶體 + 磁碟快取；支援 GIF 多幀 loop。"""

    image_ready = pyqtSignal(str)

    def __init__(self, root_dir: str = IMAGE_DIR):
        super().__init__()
        self.root = root_dir
        os.makedirs(self.root, exist_ok=True)
        self._mem: Dict[str, CachedImage] = {}
        self._inflight: Set[str] = set()
        self._failed: Set[str] = set()
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(IMAGE_MAX_CONCURRENT)

    def disk_path(self, url: str) -> str:
        return os.path.join(self.root, cache_key_for_url(url) + ".img")

    def get(self, url: str) -> Optional[CachedImage]:
        if not url or url in self._failed:
            return None
        if not image_url_safe(url):
            return None
        with self._lock:
            ci = self._mem.get(url)
        if ci is not None and ci.frames:
            return ci
        path = self.disk_path(url)
        if os.path.isfile(path) and os.path.getsize(path) > 32:
            ci = load_cached_image_from_path(path)
            if ci is not None:
                with self._lock:
                    self._mem[url] = ci
                    self._trim_mem()
                return ci
        self.request(url)
        return None

    def get_pixmap(self, url: str, t_sec: Optional[float] = None, size: Optional[QSize] = None) -> Optional[QPixmap]:
        """取目前應顯示的那一幀（GIF 依 t_sec loop）；可帶 size 用預縮放快取。"""
        ci = self.get(url)
        if ci is None or not ci.frames:
            return None
        t = 0.0 if t_sec is None else t_sec
        if size is not None and size.width() > 0 and size.height() > 0:
            pm = ci.scaled_frame_at(t, size)
        else:
            pm = ci.frame_at(t)
        return pm if pm and not pm.isNull() else None

    def request(self, url: str):
        if not url or url in self._failed:
            return
        if not image_url_safe(url):
            return
        with self._lock:
            if url in self._mem or url in self._inflight:
                return
            path = self.disk_path(url)
            if os.path.isfile(path) and os.path.getsize(path) > 32:
                pass
            else:
                self._inflight.add(url)
                t = threading.Thread(target=self._download, args=(url,), daemon=True)
                t.start()
                return
        ci = load_cached_image_from_path(path)
        if ci is not None:
            with self._lock:
                self._mem[url] = ci
                self._trim_mem()
            self.image_ready.emit(url)

    def _trim_mem(self):
        # 最多保留 64 張圖（含 GIF）
        while len(self._mem) > 64:
            self._mem.pop(next(iter(self._mem)))

    def _download(self, url: str):
        path = self.disk_path(url)
        if not self._sem.acquire(timeout=30):
            with self._lock:
                self._inflight.discard(url)
            return
        try:
            if not image_url_safe(url):
                raise ValueError("url not allowed")
            with requests.get(
                url,
                timeout=12,
                stream=True,
                allow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/gif,image/*,*/*;q=0.8",
                },
            ) as r:
                r.raise_for_status()
                # redirect 後再檢查 host
                final = r.url or url
                if not image_url_safe(final):
                    raise ValueError("redirect target not allowed")
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > IMAGE_MAX_BYTES:
                    raise ValueError(f"too large Content-Length {cl}")
                chunks = []
                total = 0
                for chunk in r.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > IMAGE_MAX_BYTES:
                        raise ValueError("image exceeds size cap")
                    chunks.append(chunk)
                data = b"".join(chunks)
            if len(data) < 32:
                raise ValueError("image too small")
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            kind = "GIF" if data[:6] in (b"GIF87a", b"GIF89a") else "IMG"
            print(f"[INFO] 圖片已快取({kind}): {url[:60]}… → {os.path.basename(path)}")
        except Exception as e:
            print(f"[INFO] 圖片下載失敗: {url[:50]}… ({e})")
            with self._lock:
                self._failed.add(url)
                self._inflight.discard(url)
            return
        finally:
            self._sem.release()
        with self._lock:
            self._inflight.discard(url)
        self.image_ready.emit(url)

    def load_into_mem(self, url: str) -> Optional[CachedImage]:
        """主線程呼叫：從 disk 載入（含 GIF 幀）。"""
        with self._lock:
            if url in self._mem:
                return self._mem[url]
        path = self.disk_path(url)
        ci = load_cached_image_from_path(path)
        if ci is None:
            return None
        with self._lock:
            self._mem[url] = ci
            self._trim_mem()
        return ci


# ===================== Danmaku overlay (UI/UX) =====================

# 可選高亮（股市閒聊常見字）
HIGHLIGHT_WORDS = (
    "融資", "跌停", "漲停", "殺盤", "反彈", "空單", "多單",
    "AI", "台積", "大盤", "期貨", "選擇權",
)

DENSITY_PRESETS = {
    # name: (lanes, row_h, min_gap, base_speed) — row 約 2 倍
    "疏": (3, 116, 120, 1.6),
    "中": (4, 104, 100, 2.0),
    "密": (6, 80, 72, 2.4),
}


@dataclass
class DanmakuItem:
    """一段連續文字：👍 user：內文（可含 [圖]）。"""
    text: str
    tag: str
    x: float
    lane: int
    speed: float
    width: float
    accent: QColor
    image_urls: List[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class PendingPush:
    tag: str
    user: str
    content: str  # 已替換成 [圖]
    image_urls: List[str]
    enqueued_at: float


def tag_prefix(tag: str) -> str:
    if tag == "推":
        return "👍"
    if tag == "噓":
        return "👎"
    return "->"


def tag_accent(tag: str) -> QColor:
    if tag == "推":
        return QColor(80, 220, 120, 250)
    if tag == "噓":
        return QColor(255, 95, 95, 250)
    return QColor(245, 245, 245, 245)


def format_danmaku_text(tag: str, user: str, content: str) -> str:
    # 連續一段：emoji + 半形空白 + user + 全形： + 內文
    return f"{tag_prefix(tag)} {user}：{content}"


def danmaku_color_for_tag(tag: str) -> QColor:
    return tag_accent(tag)


def truncate_content(content: str, max_chars: int) -> str:
    content = content.strip()
    if len(content) <= max_chars:
        return content
    return content[: max(1, max_chars - 1)].rstrip() + "…"


def session_kind_from_title(title: str) -> str:
    if "盤中" in title:
        return "盤中"
    if "盤後" in title:
        return "盤後"
    return ""


class ControlHandle(QWidget):
    """穿透模式下仍可點的小把手（選單 / 拖曳提示）。"""

    menu_requested = pyqtSignal()

    def __init__(self, parent_overlay: "DanmakuOverlay"):
        super().__init__(None)
        self._overlay = parent_overlay
        # 不用 Tool：macOS 上 Tool 在切到其他 app 會被 hide
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Window
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drag_origin: Optional[QPoint] = None
        self._win_origin: Optional[QPoint] = None

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(40, 40, 45, 210))
        p.setPen(QPen(QColor(200, 200, 210, 180), 1))
        p.drawRoundedRect(1, 1, 26, 26, 6, 6)
        p.setPen(QPen(QColor(230, 230, 235)))
        # 三點
        for i, y in enumerate((9, 14, 19)):
            p.drawEllipse(12, y, 3, 3)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint()
            self._win_origin = self._overlay.pos()
        elif event.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit()

    def mouseMoveEvent(self, event):
        if self._drag_origin is None or self._win_origin is None:
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        delta = event.globalPosition().toPoint() - self._drag_origin
        self._overlay.move(self._win_origin + delta)
        self._overlay._reposition_handle()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # 幾乎沒拖 → 當點擊開選單；有拖曳 → 吸附邊緣
            if self._drag_origin is not None:
                d = event.globalPosition().toPoint() - self._drag_origin
                if abs(d.x()) + abs(d.y()) < 4:
                    self.menu_requested.emit()
                else:
                    self._overlay.snap_to_screen_edge()
            self._drag_origin = None
            self._win_origin = None

    def mouseDoubleClickEvent(self, event):
        self.menu_requested.emit()


class DanmakuOverlay(QWidget):
    """
    半寬彈幕 + header 狀態 + 密度 + 排隊 + 分層上色 + 同人合併。
    可滑鼠拖曳整窗、靠近螢幕邊緣吸附；左上把手也可拖 / 開選單。
    """

    HEADER_H = 28
    PAD_Y = 10
    PENDING_MAX_AGE = 30.0
    MERGE_WINDOW = 2.8
    # 拖曳放開時，距邊緣小於此像素則吸附
    SNAP_THRESHOLD = 36

    def __init__(self):
        super().__init__()
        # 不用 Tool：macOS 切換到其他視窗時 Tool 會被系統藏起，看起來像 hide
        # WindowStaysOnTop + ShowWithoutActivating：留在最前、點別處也不消失
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Window
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        self.density = "中"
        self.lane_count, self.ROW_H, self.MIN_GAP, self.SPEED = DENSITY_PRESETS[self.density]
        self.active: List[DanmakuItem] = []
        self._pending: List[PendingPush] = []
        self.paused = False
        # 預設不穿透，才能拖曳視窗；要擋點擊可在選單開穿透
        self.click_through = False
        self.ui_state = "connecting"
        self.article_title = ""
        self._last_spawn_user = ""
        self._last_spawn_time = 0.0
        self._quit_callback: Optional[Callable[[], None]] = None
        # 拖曳視窗
        self._drag_origin: Optional[QPoint] = None
        self._win_origin: Optional[QPoint] = None

        self.font = QFont("PingFang TC", 16)
        self.font.setFamilies(["PingFang TC", "Apple Color Emoji", "sans-serif"])
        self.header_font = QFont("PingFang TC", 11)
        self.header_font.setFamilies(["PingFang TC", "sans-serif"])

        self.bg_color = QColor(10, 10, 12, 150)
        self.header_bg = QColor(18, 18, 22, 200)
        self.pause_color = QColor(255, 220, 80, 230)

        # 圖片快取：URL → image/ 磁碟 + 記憶體；載入後動態替換 [圖]
        self._images = ImageCache(IMAGE_DIR)
        self._images.image_ready.connect(self._on_image_ready)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

        self._handle = ControlHandle(self)
        self._handle.menu_requested.connect(self._open_menu_at_handle)
        self._handle.show()

        self._apply_click_through()
        self._layout_half_screen()

    # ----- public API -----

    def set_quit_callback(self, cb: Callable[[], None]):
        self._quit_callback = cb

    def set_ui_state(self, state: str):
        if state != self.ui_state:
            self.ui_state = state
            self.update()

    def set_article_title(self, title: str):
        title = (title or "").strip()
        if title and title != self.article_title:
            self.article_title = title
            print(f"[INFO] 追蹤文章：{title}")
            self.update()

    def add_push(self, tag: str, user: str, content: str):
        """推文入口：URL→[圖]、合併、入隊；完整顯示內文（不截斷）。"""
        content, image_urls = extract_image_urls_and_placeholder(content)
        content = content.strip()
        if not content and not image_urls:
            return
        if not content and image_urls:
            content = IMG_PLACEHOLDER * len(image_urls)

        # 觸發下載（磁碟已有則直接 load）
        for u in image_urls:
            self._images.request(u)
            self._images.get(u)  # 嘗試同步從 disk 載入

        now = time.time()
        # 同人短時間連發 → 合併到 pending 最後一則（同 user）
        if (
            self._pending
            and self._pending[-1].user == user
            and now - self._pending[-1].enqueued_at < self.MERGE_WINDOW
        ):
            prev = self._pending[-1]
            prev.content = f"{prev.content} ｜ {content}"
            prev.image_urls = list(prev.image_urls) + list(image_urls)
            prev.tag = tag if tag == "噓" else prev.tag
            prev.enqueued_at = now
            return
        self._pending.append(
            PendingPush(
                tag=tag,
                user=user,
                content=content,
                image_urls=list(image_urls),
                enqueued_at=now,
            )
        )
        self._drop_stale_pending(now)

    def _on_image_ready(self, url: str):
        """背景下載完成 → 主線程載入（含 GIF 幀），重算進行中彈幕寬度並刷新。"""
        ci = self._images.load_into_mem(url)
        if ci is None or not ci.frames:
            return
        if ci.is_animated:
            print(f"[INFO] GIF {len(ci.frames)} 幀 loop 播放: {url[:50]}…")
        changed = False
        for item in self.active:
            if url in item.image_urls:
                item.width = self._measure_line_with_images(item.text, item.image_urls)
                changed = True
        if changed or ci.is_animated:
            self.update()

    def add_comment(self, text: str, color: Optional[QColor] = None):
        # 相容舊介面：粗拆 tag
        tag = "→"
        if text.startswith("👍"):
            tag = "推"
            text = text[1:].lstrip()
        elif text.startswith("👎"):
            tag = "噓"
            text = text[1:].lstrip()
        elif text.startswith("->"):
            text = text[2:].lstrip()
        if "：" in text:
            user, _, content = text.partition("：")
        else:
            user, _, content = text.partition(":")
        self.add_push(tag, user.strip() or "?", content.strip() or text)

    def set_paused(self, paused: bool):
        self.paused = paused
        print(f"[INFO] 彈幕{'暫停' if paused else '繼續'}")
        self.update()

    def toggle_pause(self):
        self.set_paused(not self.paused)

    def set_density(self, name: str):
        if name not in DENSITY_PRESETS:
            return
        self.density = name
        self.lane_count, self.ROW_H, self.MIN_GAP, self.SPEED = DENSITY_PRESETS[name]
        # 超過 lane 的彈幕清到不顯示（避免 index 越界）
        self.active = [a for a in self.active if a.lane < self.lane_count]
        self._layout_half_screen()
        print(f"[INFO] 密度：{name}（{self.lane_count} 列）")
        self.update()

    def set_click_through(self, enabled: bool):
        self.click_through = enabled
        self._apply_click_through()
        print(f"[INFO] 滑鼠穿透：{'開' if enabled else '關'}")

    def toggle_click_through(self):
        self.set_click_through(not self.click_through)

    # ----- layout / input -----

    def _apply_click_through(self):
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self.click_through)
        # 把手永遠可點（穿透時靠它拖曳 / 開選單）
        self._handle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._handle.raise_()
        if not self.click_through:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def _layout_half_screen(self):
        screen = QApplication.primaryScreen().geometry()
        w = max(360, screen.width() // 2)
        h = self.HEADER_H + self.PAD_Y * 2 + self.lane_count * self.ROW_H
        self.resize(w, h)
        self.move((screen.width() - w) // 2, int(screen.height() * 0.07))
        self._reposition_handle()

    def _reposition_handle(self):
        if not self._handle:
            return
        # 貼在視窗左上外側一點
        gp = self.mapToGlobal(QPoint(4, 4))
        self._handle.move(gp.x() - 32, gp.y())

    def _screen_geo_for_window(self):
        """視窗中心所在螢幕的可用區域（避開選單列 / Dock）。"""
        center = self.frameGeometry().center()
        scr = QApplication.screenAt(center) or QApplication.primaryScreen()
        return scr.availableGeometry()

    def snap_to_screen_edge(self):
        """靠近邊緣時吸附；可同時吸附水平+垂直（角落）。"""
        geo = self._screen_geo_for_window()
        x, y = self.x(), self.y()
        w, h = self.width(), self.height()
        thr = self.SNAP_THRESHOLD

        # 水平
        if abs(x - geo.left()) <= thr:
            x = geo.left()
        elif abs((x + w) - (geo.right() + 1)) <= thr or abs((x + w) - geo.right()) <= thr:
            x = geo.right() - w + 1
        # 垂直
        if abs(y - geo.top()) <= thr:
            y = geo.top()
        elif abs((y + h) - (geo.bottom() + 1)) <= thr or abs((y + h) - geo.bottom()) <= thr:
            y = geo.bottom() - h + 1

        # 確保整窗仍在可用區域內
        x = max(geo.left(), min(x, geo.right() - w + 1))
        y = max(geo.top(), min(y, geo.bottom() - h + 1))

        if x != self.x() or y != self.y():
            self.move(x, y)
            self._reposition_handle()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._reposition_handle()

    def showEvent(self, event):
        super().showEvent(event)
        self._handle.show()
        self._reposition_handle()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._handle.hide()

    def closeEvent(self, event):
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            if self._handle:
                self._handle.close()
        except Exception:
            pass
        super().closeEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.click_through:
            self._drag_origin = event.globalPosition().toPoint()
            self._win_origin = self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self._drag_origin is not None
            and self._win_origin is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
            and not self.click_through
        ):
            delta = event.globalPosition().toPoint() - self._drag_origin
            self.move(self._win_origin + delta)
            self._reposition_handle()
            event.accept()
            return
        # header 上顯示可拖提示
        if not self.click_through and event.position().y() <= self.HEADER_H:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif not self.click_through:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            dragged = False
            if self._drag_origin is not None:
                d = event.globalPosition().toPoint() - self._drag_origin
                dragged = abs(d.x()) + abs(d.y()) >= 4
            self._drag_origin = None
            self._win_origin = None
            if dragged and not self.click_through:
                self.snap_to_screen_edge()
            if not self.click_through:
                if event.position().y() <= self.HEADER_H:
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
                else:
                    self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        # Alt 放開穿透時：按 P 暫停、Esc 選單
        if event.key() == Qt.Key.Key_P:
            self.toggle_pause()
        elif event.key() == Qt.Key.Key_Escape:
            self._popup_menu(self.mapToGlobal(QPoint(20, 20)))
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        # 非穿透 或 Alt 持下
        mods = event.modifiers()
        if (not self.click_through) or (mods & Qt.KeyboardModifier.AltModifier):
            self._popup_menu(event.globalPos())
            event.accept()
        else:
            event.ignore()

    def _on_context_menu(self, pos: QPoint):
        if not self.click_through:
            self._popup_menu(self.mapToGlobal(pos))

    def _open_menu_at_handle(self):
        self._popup_menu(self._handle.mapToGlobal(QPoint(0, self._handle.height())))

    def _popup_menu(self, global_pos: QPoint):
        # 開選單時暫時關閉穿透，否則 menu 點不到
        was = self.click_through
        if was:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        menu = QMenu(self)
        act_pause = QAction("繼續" if self.paused else "暫停", self)
        act_pause.triggered.connect(self.toggle_pause)
        menu.addAction(act_pause)

        act_through = QAction("關閉滑鼠穿透" if self.click_through else "開啟滑鼠穿透", self)
        act_through.triggered.connect(self.toggle_click_through)
        menu.addAction(act_through)

        dens = menu.addMenu("密度")
        for name in ("疏", "中", "密"):
            act = QAction(("✓ " if name == self.density else "  ") + name, self)
            act.triggered.connect(lambda checked=False, n=name: self.set_density(n))
            dens.addAction(act)

        menu.addSeparator()
        act_quit = QAction("退出", self)
        if self._quit_callback:
            act_quit.triggered.connect(self._quit_callback)
        else:
            act_quit.triggered.connect(self.close)
        menu.addAction(act_quit)

        menu.exec(global_pos)

        if was:
            self._apply_click_through()

    # ----- pending / spawn -----

    def _drop_stale_pending(self, now: Optional[float] = None):
        now = now or time.time()
        before = len(self._pending)
        self._pending = [p for p in self._pending if now - p.enqueued_at <= self.PENDING_MAX_AGE]
        dropped = before - len(self._pending)
        if dropped:
            print(f"[INFO] 丟棄過舊推文 {dropped} 則")

    def _measure(self, text: str, font: Optional[QFont] = None) -> float:
        """量字寬：寧可偏寬一點，避免裁切。"""
        if not text:
            return 0.0
        f = font or self.font
        fm = QFontMetrics(f)
        w = max(
            fm.horizontalAdvance(text),
            fm.boundingRect(text).width(),
            fm.size(0, text).width(),
        )
        for ch in text:
            o = ord(ch)
            if o >= 0x1F300 or 0x2600 <= o <= 0x27BF or 0xFE00 <= o <= 0xFE0F:
                w += 8
        return float(w + 4)

    def _draw_text_full(
        self,
        painter: QPainter,
        x: float,
        y: float,
        text: str,
        row_h: float,
    ):
        """不裁切完整畫文字。"""
        if not text:
            return
        flags = int(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
            | Qt.TextFlag.TextSingleLine
            | Qt.TextFlag.TextDontClip
        )
        painter.drawText(QRectF(x, y, 8192, row_h), flags, text)

    def _image_display_size(self, url: str) -> QSize:
        """圖高度 = row 高；未載入則用 [圖] 文字寬。"""
        h = max(16, self.ROW_H - 8)
        ci = self._images.get(url)
        pm = ci.first() if ci else None
        if pm is None or pm.isNull():
            return QSize(int(self._measure(IMG_PLACEHOLDER)), h)
        src_w = max(1, pm.width())
        src_h = max(1, pm.height())
        w = max(12, int(src_w * (h / src_h)))
        w = min(w, int(self.width() * 0.35))
        return QSize(w, h)

    def _measure_line_with_images(self, text: str, image_urls: List[str]) -> float:
        """整段連續字串量寬；僅在 [圖] 處插入圖寬。"""
        parts = text.split(IMG_PLACEHOLDER)
        total = 0.0
        img_i = 0
        for i, part in enumerate(parts):
            if part:
                total += self._measure(part)
            if i < len(parts) - 1:
                if img_i < len(image_urls):
                    total += float(self._image_display_size(image_urls[img_i]).width())
                    img_i += 1
                else:
                    total += self._measure(IMG_PLACEHOLDER)
        return total

    def _lane_right_edge(self, lane: int) -> float:
        edge = float("-inf")
        for it in self.active:
            if it.lane == lane:
                edge = max(edge, it.x + it.width)
        return edge

    def _find_free_lane(self, spawn_x: float) -> Optional[int]:
        for i in range(self.lane_count):
            right = self._lane_right_edge(i)
            if right == float("-inf") or spawn_x >= right + self.MIN_GAP:
                return i
        return None

    def _spawn_pending(self, p: PendingPush) -> bool:
        content = p.content
        for kw in HIGHLIGHT_WORDS:
            if kw in content and IMG_PLACEHOLDER not in kw:
                content = content.replace(kw, f"〔{kw}〕", 1)
                break

        # 一段連續文字：👍 user：內文
        text = format_danmaku_text(p.tag, p.user, content)
        total_w = self._measure_line_with_images(text, p.image_urls)

        spawn_x = float(self.width())
        lane = self._find_free_lane(spawn_x)
        if lane is None:
            return False

        now = time.time()
        self.active.append(
            DanmakuItem(
                text=text,
                tag=p.tag,
                x=spawn_x,
                lane=lane,
                speed=self.SPEED,
                width=total_w,
                accent=tag_accent(p.tag),
                image_urls=list(p.image_urls),
                created_at=now,
            )
        )
        self._last_spawn_user = p.user
        self._last_spawn_time = now
        return True

    def _tick(self):
        now = time.time()
        self._drop_stale_pending(now)

        if not self.paused:
            still: List[DanmakuItem] = []
            for item in self.active:
                item.x -= item.speed if item.speed else self.SPEED
                if item.x + item.width >= -12:
                    still.append(item)
            self.active = still

            # 每 tick 最多 1 則
            if self._pending:
                p = self._pending[0]
                if self._spawn_pending(p):
                    self._pending.pop(0)

        self._reposition_handle()
        # 每幀重畫：彈幕位移 + GIF 依時間 loop
        self.update()

    # ----- paint -----

    def _state_color(self) -> QColor:
        return {
            "connecting": QColor(255, 200, 60),
            "reconnecting": QColor(255, 200, 60),
            "navigating": QColor(255, 200, 60),
            "live": QColor(80, 220, 120),
            "error": QColor(255, 90, 90),
            "disconnected": QColor(160, 160, 160),
        }.get(self.ui_state, QColor(160, 160, 160))

    def _session_strip_color(self) -> QColor:
        kind = session_kind_from_title(self.article_title)
        if kind == "盤中":
            return QColor(70, 150, 255, 220)
        if kind == "盤後":
            return QColor(255, 150, 70, 220)
        return QColor(120, 120, 130, 180)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # 背景
        painter.fillRect(self.rect(), self.bg_color)

        # 左側時段色條
        painter.fillRect(0, 0, 3, self.height(), self._session_strip_color())

        # Header
        painter.fillRect(0, 0, self.width(), self.HEADER_H, self.header_bg)
        # 狀態點
        painter.setBrush(self._state_color())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(10, 9, 10, 10)

        painter.setFont(self.header_font)
        painter.setPen(QPen(QColor(230, 230, 235, 240)))
        title = self.article_title or "Stock · 尋找盤中/盤後…"
        state_label = {
            "connecting": "連線中",
            "reconnecting": "重連中",
            "navigating": "導航中",
            "live": "LIVE",
            "error": "錯誤",
            "disconnected": "已斷線",
        }.get(self.ui_state, self.ui_state)
        header = f"  Stock · {title}  ·  {state_label}"
        if self.paused:
            header += "  ·  暫停"
        qn = len(self._pending)
        if qn:
            header += f"  ·  +{qn}"
        painter.drawText(
            QRectF(22, 0, self.width() - 30, self.HEADER_H),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            header,
        )

        # 彈幕：一段連續文字；[圖] 處播靜態圖或 GIF loop（隨彈幕移動持續播）
        painter.setFont(self.font)
        painter.setClipping(False)
        body_top = self.HEADER_H + self.PAD_Y
        now = time.time()
        for item in self.active:
            y = body_top + item.lane * self.ROW_H
            # 以彈幕存活時間當動畫時軸 → 飛行期間 loop
            anim_t = max(0.0, now - item.created_at)
            self._paint_line_with_images(
                painter, item.x, y, item.text, item.image_urls, item.accent, anim_t
            )

    def _paint_line_with_images(
        self,
        painter: QPainter,
        x: float,
        y: float,
        text: str,
        image_urls: List[str],
        accent: QColor,
        anim_t: float = 0.0,
    ) -> float:
        """連續字串；[圖] 插入圖片。GIF 用 anim_t loop 換幀。"""
        parts = text.split(IMG_PLACEHOLDER)
        img_i = 0
        img_h = max(16, self.ROW_H - 8)
        img_y = y + (self.ROW_H - img_h) / 2.0
        painter.setPen(QPen(accent))
        for i, part in enumerate(parts):
            if part:
                w = self._measure(part)
                self._draw_text_full(painter, x, y, part, self.ROW_H)
                x += w
            if i < len(parts) - 1:
                url = image_urls[img_i] if img_i < len(image_urls) else ""
                img_i += 1
                sz = self._image_display_size(url) if url else QSize(0, 0)
                pm = self._images.get_pixmap(url, anim_t, size=sz) if url else None
                if pm is not None and not pm.isNull():
                    painter.drawPixmap(int(x), int(img_y), pm)
                    x += pm.width()
                else:
                    w = self._measure(IMG_PLACEHOLDER)
                    self._draw_text_full(painter, x, y, IMG_PLACEHOLDER, self.ROW_H)
                    x += w
        return x


# ===================== main =====================

def main():
    print("[INFO] PTT Stock 彈幕（畫面驅動導航）")
    print("[INFO] 載入帳號密碼中...")

    acc, pwd = load_ptt_credentials()
    if acc:
        print(f"[INFO] 將以帳號登入：{acc}")
    else:
        print("[INFO] 未找到帳號密碼，將以 guest 嘗試（建議 .pttrc）")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    overlay = DanmakuOverlay()
    client = PTTWebSocketClient()

    def log_info(msg: str):
        print(f"[INFO] {msg}")

    def log_push(push: Push):
        shown, urls = extract_image_urls_and_placeholder(push.content)
        text = format_danmaku_text(push.tag, push.user, shown)
        if urls:
            print(f"[PUSH] {text}  (img×{len(urls)})")
        else:
            print(f"[PUSH] {text}")
        overlay.add_push(push.tag, push.user, push.content)

    def on_ui_state(state: str):
        overlay.set_ui_state(state)

    def on_title(title: str):
        overlay.set_article_title(title)

    client.status.connect(log_info)
    client.new_push.connect(log_push)
    client.ui_state.connect(on_ui_state)
    client.article_title.connect(on_title)

    print("[INFO] 開始連線 WebSocket...")
    client.start()
    overlay.show()

    cleaned = {"done": False}

    def cleanup():
        if cleaned["done"]:
            return
        cleaned["done"] = True
        print("[INFO] 正在關閉...")
        try:
            client.stop()
        except Exception as e:
            print(f"[INFO] stop 例外: {e}")
        try:
            overlay._timer.stop()
        except Exception:
            pass
        try:
            overlay._handle.close()
        except Exception:
            pass

    def request_quit():
        cleanup()
        app.quit()

    overlay.set_quit_callback(request_quit)
    app.aboutToQuit.connect(cleanup)

    _orig_close = overlay.closeEvent

    def on_close(event):
        cleanup()
        try:
            _orig_close(event)
        except Exception:
            event.accept()
        QTimer.singleShot(0, app.quit)

    overlay.closeEvent = on_close

    print("[INFO] UI：半寬 / 可拖曳視窗 / header 狀態 / 密度。左上把手亦可拖；選單可開穿透。")
    rc = app.exec()
    cleanup()
    print("[INFO] 程式結束")
    sys.exit(rc)


if __name__ == "__main__":
    main()
