# WooTalkBot

WooTalk 自動化桌面工具。Windows 免安裝，雙擊 `WooTalkBot.exe` 即可。

## 功能

- 自動配對聊天
- 偵測封鎖關鍵字（對方前 3 句）自動離開換人
- 配對成功自動送出第一句

## 下載

到 [Releases](https://github.com/aaaagent0614-art/WooTalkBot/releases) 下載最新 `WooTalkBot.exe`。

> Windows SmartScreen 可能擋「未知發行者」的 exe，點「仍要執行」即可（本專案未做程式碼簽章）。

## 從原始碼執行

```
pip install websockets
python wootalk_app.py
```
