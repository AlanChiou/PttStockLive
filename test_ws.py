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
你可以直接在這個終端輸入要送的鍵（例如直接按 Enter 送空白鍵，或輸入 F 再輸入檔名）。
"""
import re
import sys
import threading
import time

import websocket
from websocket import WebSocketConnectionClosedException

from ptt_common import encode_login_field, env_kick_other_sessions, load_ptt_credentials, try_decode

PTT_WS_URL = "wss://ws.ptt.cc/bbs"
ORIGIN = "https://term.ptt.cc"


def extract_filename(url: str):
    m = re.search(r"(M\.[A-Za-z0-9.]+)\.html?", url or "")
    return m.group(1) if m else None


def main():
    target_url = sys.argv[1] if len(sys.argv) > 1 else None
    filename = extract_filename(target_url)
    account, password = load_ptt_credentials()
    kick_other = env_kick_other_sessions()

    print(f"目標文章檔名: {filename or '(未指定)'}")
    if account:
        print(f"偵測到帳號：{account}（將自動登入）")
        print(f"重複登入處理：{'踢掉其他連線 (y)' if kick_other else '保留其他連線 (n)'}  [PTT_KICK_OTHER]")
    else:
        print("未偵測到帳號，將以 guest 模式連線")
    print("連線中... (按 Ctrl-C 結束，輸入 q 離開)\n")

    stop = False
    account_sent = False
    password_sent = False
    dup_handled = False

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
                except Exception:
                    pass
                break
            if line == "":
                ws_obj.send(b" ", websocket.ABNF.OPCODE_BINARY)
            else:
                try:
                    ws_obj.send(line.encode("latin-1", errors="replace"), websocket.ABNF.OPCODE_BINARY)
                except Exception as e:
                    print("[SEND ERR]", e)

    ws = None
    try:
        ws = websocket.WebSocket()
        ws.connect(
            PTT_WS_URL,
            origin=ORIGIN,
            header=[
                "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
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

                if account:
                    if not account_sent and (
                        "請輸入代號" in text or "請輸入您的代號" in text
                    ):
                        b, err = encode_login_field(account)
                        if err:
                            print(f"[AUTO] 帳號無法編碼: {err}\n")
                        else:
                            ws.send(b + b"\r", websocket.ABNF.OPCODE_BINARY)
                            account_sent = True
                            print(f"[AUTO] 送出帳號 {account}\n")
                    elif (
                        account_sent
                        and not password_sent
                        and any(p in text for p in ("請輸入密碼", "請輸入您的密碼"))
                    ):
                        b, err = encode_login_field(password or "")
                        if err:
                            print(f"[AUTO] 密碼無法編碼: {err}\n")
                        else:
                            ws.send(b + b"\r", websocket.ABNF.OPCODE_BINARY)
                            password_sent = True
                            print("[AUTO] 送出密碼\n")
                    elif (
                        not dup_handled
                        and (
                            "您有其它連線已登入此帳號" in text
                            or "刪除其他重複登入" in text
                            or ("重複登入" in text and "[Y/n]" in text)
                        )
                    ):
                        ans = b"y\r" if kick_other else b"n\r"
                        ws.send(ans, websocket.ABNF.OPCODE_BINARY)
                        dup_handled = True
                        print(
                            f"[AUTO] 重複登入提示 → 送 {'y' if kick_other else 'n'}\n"
                        )
            except WebSocketConnectionClosedException:
                break
            except Exception as recv_e:
                err = str(recv_e).lower()
                if "timeout" in err or "timed out" in err:
                    continue
                print("[RECV ERR]", recv_e)
                break

    except Exception as e:
        print("[ERROR]", str(e)[:300])
    finally:
        stop = True
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        print("\n結束")


if __name__ == "__main__":
    main()
