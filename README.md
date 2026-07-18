# PTT Stock 彈幕

即時顯示 PTT Stock 版「盤中 / 盤後」閒聊串推文的桌面彈幕（Python + PyQt6 + WebSocket）。

## 特色

- 純 `wss://ws.ptt.cc/bbs` 終端串流（不爬 web 版）
- 畫面驅動導航：登入 → Stock → 搜尋盤中/盤後 → 進文 → END 到底
- Live 刷新語意對齊 [PttChrome Live 文小幫手](https://github.com/iamchucky/PttChrome)（LEFT 回列表後重定位再進）
- 半寬置頂彈幕窗、可拖曳；推綠 / 噓紅 / `->` 白
- 推文圖片 URL → `[圖]` placeholder，下載快取到 `image/` 後動態替換
- 右鍵／左上把手選單：暫停、穿透、密度、退出

## 需求

- Python 3.9+
- macOS / Linux（Windows 可跑但字型與穿透行為可能不同）

## 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

若 `uao` 安裝失敗可忽略（會 fallback 到 big5hkscs / big5）。

## 帳號設定

**不要把帳密寫進程式或提交到 git。**

```bash
cp .pttrc.example .pttrc
# 編輯 .pttrc
export PTT_ACCOUNT=你的帳號
export PTT_PASSWORD=你的密碼
```

或直接：

```bash
export PTT_ACCOUNT=你的帳號
export PTT_PASSWORD=你的密碼
```

## 執行

```bash
source .pttrc
python3 ptt_danmaku.py
```

啟動後會：

1. 連線 WebSocket 並登入
2. 進入 Stock、搜尋盤中/盤後閒聊並進入
3. 推文輸出到 console（`[PUSH]`）並飛上彈幕

關閉視窗或選單「退出」結束程式。

### 操作

| 操作 | 說明 |
|------|------|
| 拖曳視窗 | 左鍵按住拖動（預設關閉穿透） |
| 左上把手 | 拖曳／開選單 |
| 右鍵選單 | 暫停、滑鼠穿透、密度（疏/中/密）、退出 |

### 除錯工具

```bash
python3 test_ws.py
# 或指定文章
python3 test_ws.py "https://www.ptt.cc/bbs/Stock/M.xxxx.A.xxx.html"
```

## 注意

- 需要有效 PTT 帳密；guest 導航成功率低
- WS 連線可能中斷，程式會自動重試
- 圖片快取目錄 `image/` 為執行期產物，已在 `.gitignore`
- 請遵守 PTT 站規，勿高頻洗連線

## 授權

MIT
