#!/usr/bin/env python3
"""
PTT WebSocket 原始連線測試工具
用來觀察 wss://ws.ptt.cc/bbs 回傳的原始畫面，幫助你手動導航或除錯彈幕程式。

支援與主程式相同的登入方式：
- 環境變數 PTT_ACCOUNT / PTT_PASSWORD
- .pttrc / ~/.pttrc （可 source 的格式）

用法：
    python3 test_ws.py
    python3 test_ws.py "https://www.ptt.cc/bbs/Stock/M.1780xxxx.A.xxx.html"

連線後會印出收到的文字片段。
你可以直接在這個終端輸入要送的鍵（例如直接按 Enter 送 \n，或輸入 F 再輸入檔名）。
"""
import sys
import re
import time
import threading
import os
from typing import Tuple, Optional

import websocket
from websocket import WebSocketConnectionClosedException

PTT_WS_URL = "wss://ws.ptt.cc/bbs"
ORIGIN = "https://term.ptt.cc"


def load_ptt_credentials() -> Tuple[Optional[str], Optional[str]]:
    """與主程式相同的載入邏輯"""
    account = os.environ.get("PTT_ACCOUNT") or os.environ.get("PTT_ID")
    password = os.environ.get("PTT_PASSWORD") or os.environ.get("PTT_PASS")
    if account and password:
        return account, password

    candidates = [".pttrc", os.path.expanduser("~/.pttrc"), ".env"]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            env = {}
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"\'')
            acc = env.get("PTT_ACCOUNT") or env.get("PTT_ID")
            pwd = env.get("PTT_PASSWORD") or env.get("PTT_PASS")
            if acc and pwd:
                return acc, pwd
        except Exception:
            continue
    return None, None

def try_decode(data: bytes) -> str:
    try:
        import uao
        return data.decode("uao", errors="replace")
    except Exception:
        pass
    for enc in ("big5hkscs", "big5", "cp950"):
        try:
            return data.decode(enc, errors="replace")
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")

def extract_filename(url: str):
    m = re.search(r'(M\.[A-Za-z0-9.]+)\.html?', url or "")
    return m.group(1) if m else None

def main():
    target_url = sys.argv[1] if len(sys.argv) > 1 else None
    filename = extract_filename(target_url)
    account, password = load_ptt_credentials()

    print(f"目標文章檔名: {filename or '(未指定)'}")
    if account:
        print(f"偵測到帳號：{account}（將自動登入）")
    else:
        print("未偵測到帳號，將以 guest 模式連線")
    print("連線中... (按 Ctrl-C 結束，輸入 q 離開)\n")

    stop = False

    def input_loop(ws_obj):
        nonlocal stop
        print("輸入要送的字串（直接 Enter 送空白鍵，輸入 q 結束）：")
        while not stop:
            try:
                line = input("> ").rstrip("\n")
            except EOFError:
                break
            if line.lower() in ("q", "quit", "exit"):
                stop = True
                try:
                    ws_obj.close()
                except:
                    pass
                break
            if line == "":
                ws_obj.send(" ")
            else:
                ws_obj.send(line)

    ws = None
    try:
        ws = websocket.WebSocket()
        ws.connect(
            PTT_WS_URL,
            origin=ORIGIN,
            header=[
                "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            ],
            timeout=10,
        )
        print("[OPEN] 已連線到 PTT WebSocket (低階模式)\n")

        t = threading.Thread(target=input_loop, args=(ws,), daemon=True)
        t.start()

        while not stop:
            try:
                message = ws.recv()
                if message is None:
                    break
                if isinstance(message, bytes):
                    text = try_decode(message)
                else:
                    text = str(message)
                cleaned = text.replace("\x1b", "^[").replace("\r", "")[:800]
                print("--- RECV ---")
                print(cleaned)
                print("------------\n")

                # 自動登入 (binary frame + \r for PTT)
                if account:
                    if "請輸入代號" in text or "請輸入您的代號" in text:
                        ws.send((account + "\r").encode("ascii"), websocket.ABNF.OPCODE_BINARY)
                        print(f"[AUTO] 送出帳號 {account}\n")
                    elif any(p in text for p in ["請輸入密碼", "請輸入您的密碼"]):
                        ws.send((password + "\r").encode("ascii"), websocket.ABNF.OPCODE_BINARY)
                        print("[AUTO] 送出密碼\n")
                    elif "您有其它連線已登入此帳號" in text or "[Y/n]" in text:
                        ws.send("y\r".encode("ascii"), websocket.ABNF.OPCODE_BINARY)
                        print("[AUTO] 選擇刪除其他重複登入連線\n")
            except WebSocketConnectionClosedException:
                break
            except Exception as recv_e:
                err = str(recv_e).lower()
                if "timeout" in err or "timed out" in err:
                    continue  # keep waiting for diffs
                print("[RECV ERR]", recv_e)
                break

    except Exception as e:
        print("[ERROR]", str(e)[:300])
    finally:
        stop = True
        if ws:
            try:
                ws.close()
            except:
                pass
        print("\n結束")

if __name__ == "__main__":
    main()
