#!/usr/bin/env python3
"""WooTalk 自動化 — Windows 桌面版（CustomTkinter + 可自動配對 + 手動聊天）。

依賴：websockets, customtkinter
    pip install websockets customtkinter
    python wootalk_app.py

打包 exe：
    pip install pyinstaller
    pyinstaller -F -w -n WooTalkBot wootalk_app.py
"""
import asyncio
import http.cookiejar
import json
import os
import queue
import random
import re
import sys
import threading
import urllib.request
import webbrowser
import customtkinter as ctk

import websockets

WSS = "wss://wootalk.today/websocket"
HOME = "https://wootalk.today/"

FONT = "Microsoft JhengHei UI"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def fresh_session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
    op.open(HOME)
    for c in cj:
        if c.name == "_wootalk_session":
            return c.value
    return None


def config_path():
    if getattr(sys, "frozen", False):            # PyInstaller 打包後
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "wootalk_config.json")


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _fmt_num(x):
    try:
        n = float(x)
        return str(int(n)) if n == int(n) else str(n)
    except Exception:
        return str(x)


class WooCore:
    """wootalk 連線邏輯，跑在獨立 thread 的 asyncio loop。
    事件用 (kind, text) 拋回 GUI；kind ∈ system/me/them/match/leave/verify。"""

    def __init__(self):
        self.log_q = queue.Queue()
        self.loop = None
        self.thread = None
        self.ws = None
        self.running = False
        self.matched = False

        # 設定
        self.ban = ["男", "女"]
        self.first = "安安你好"
        self.match_mode = "contains"   # contains | exact
        self.max_check = 3
        self.leave_delay_max = 5.0     # 命中封鎖字後，1~x 秒隨機延遲再離開

        # 執行期
        self.round = 0
        self.their_msgs = 0
        self.sent_first = False
        self.msg_id = 0

    # ---- GUI 端控制 ----
    def start(self, settings: dict):
        if self.running:
            return
        self.ban = settings.get("ban", [])
        self.first = settings.get("first", "")
        self.match_mode = settings.get("match_mode", "contains")
        self.max_check = int(settings.get("max_check", 3))
        self.leave_delay_max = max(1.0, float(settings.get("leave_delay_max", 5.0)))
        self.round = 0
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self._close_ws)

    def send_chat(self, text):
        text = (text or "").strip()
        if not text:
            return
        if not self.running:
            self.log(("system", "⚠️ 還沒開始配對"))
            return
        if not self.matched:
            self.log(("system", "⚠️ 還沒配對到人，訊息送不出去"))
            return
        self.log(("me", text))
        if self.loop:
            self.loop.call_soon_threadsafe(self._queue_send, text)

    # ---- asyncio 端 ----
    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())
        self.loop.close()
        self.loop = None

    def _close_ws(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _queue_send(self, text):
        asyncio.ensure_future(self._do_send(text))

    async def _do_send(self, text):
        try:
            await self._say(text)
        except Exception as e:
            self.log(("system", f"⚠️ 送出失敗: {e}"))

    def log(self, item):
        self.log_q.put(item)

    def _rid(self):
        return random.randint(100000, 999999)

    def _hit(self, text, kw):
        if self.match_mode == "exact":
            return text.strip() == kw.strip()
        return kw in text

    async def _send(self, name, data=None):
        frame = [name, {"id": self._rid(), "data": data}] if data is not None else [name, {}]
        await self.ws.send(json.dumps(frame, ensure_ascii=False))

    async def _say(self, text):
        self.msg_id += 1
        await self._send("new_message", {"message": text, "msg_id": self.msg_id})

    async def _leave_and_reconnect(self, reason):
        self.matched = False
        delay = random.uniform(1.0, self.leave_delay_max)
        self.log(("system", f"{reason}（{delay:.1f} 秒後離開）"))
        await asyncio.sleep(delay)
        await self._send("change_person")
        await self.ws.close()

    async def _handle(self, ev):
        name = ev[0]
        attrs = ev[1] if len(ev) > 1 else {}
        data = attrs.get("data") if isinstance(attrs, dict) else attrs

        if name == "websocket_rails.ping":
            await self._send("websocket_rails.pong")
        elif name == "new_message":
            msgs = data if isinstance(data, list) else [data]
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                sender = m.get("sender")
                text = m.get("message") or ""
                status = m.get("status")
                if sender == 1:
                    continue
                if sender == 0:
                    if "要繼續使用" in text:
                        m = re.search(r"https://wootalk\.today/verify/[a-zA-Z0-9-]+", text)
                        url = m.group(0) if m else None
                        self.log(("verify", "🚫 觸發 wootalk 防機器人驗證"))
                        if url:
                            self.log(("verify", f"驗證連結：{url}"))
                            try:
                                webbrowser.open(url)
                                self.log(("system", "已自動開瀏覽器"))
                            except Exception:
                                self.log(("system", "請手動複製上面的連結開瀏覽器"))
                        self.log(("system", "驗證流程：勾我不是機器人 → 等倒數 → 按我同意 → 回原分頁重整"))
                        self.log(("system", "⏸ 配對已暫停。驗證完成後，回 app 按「▶ 開始配對」繼續"))
                        self.matched = False
                        self.running = False          # 完全停止，不再自動重連
                        await self.ws.close()
                        return
                    if status == "chat_started" and not self.sent_first:
                        self.sent_first = True
                        self.matched = True
                        self.log(("match", "✅ 已配對，可以開始聊天"))
                        await asyncio.sleep(0.4)
                        if self.first:
                            await self._say(self.first)
                            self.log(("me", self.first))
                    elif status == "chat_otherleave":
                        await self._leave_and_reconnect("👋 對方離開，換下一個")
                        return
                else:  # 陌生人
                    self.their_msgs += 1
                    hit = [k for k in self.ban if k and self._hit(text, k)]
                    if hit and self.their_msgs <= self.max_check:
                        self.log(("them", text))
                        await self._leave_and_reconnect(
                            f"⛔ 前{self.max_check}句命中封鎖字 {hit}")
                        return
                    self.log(("them", text))

    async def _round(self):
        sess = fresh_session()
        if not sess:
            self.log(("system", "⚠️ 拿不到 session cookie，稍後重試"))
            return
        headers = {"Origin": HOME.rstrip("/"), "Cookie": f"_wootalk_session={sess}"}
        async with websockets.connect(WSS, origin=HOME.rstrip("/"),
                                      additional_headers=headers) as ws:
            self.ws = ws
            async for raw in ws:
                if not self.running:
                    return
                try:
                    batch = json.loads(raw)
                except Exception:
                    continue
                evs = batch if isinstance(batch, list) and batch and isinstance(batch[0], list) else [batch]
                for ev in evs:
                    await self._handle(ev)

    async def _main(self):
        while self.running:
            self.round += 1
            self.their_msgs = 0
            self.sent_first = False
            self.matched = False
            self.log(("system", f"── 第 {self.round} 輪配對 ──"))
            try:
                await self._round()
            except websockets.ConnectionClosed:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(("system", f"⚠️ {type(e).__name__}: {e}"))
            await asyncio.sleep(2)
        self.matched = False
        self.log(("system", "⏸ 已停止"))


class App:
    def __init__(self, core: WooCore):
        self.core = core
        self.root = ctk.CTk()
        self.root.title("WooTalk")
        self.root.geometry("820x600")
        self.root.minsize(720, 520)

        # 左側：設定側邊欄
        self.side = ctk.CTkFrame(self.root, width=230, corner_radius=0)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)

        cfg = load_config()

        ctk.CTkLabel(self.side, text="設定", font=(FONT, 16, "bold")).pack(anchor="w", padx=16, pady=(16, 8))

        ctk.CTkLabel(self.side, text="封鎖字（逗號分隔）", font=(FONT, 12),
                     text_color="#8a8a9e").pack(anchor="w", padx=16)
        self.ban_entry = ctk.CTkEntry(self.side, font=(FONT, 13), placeholder_text="男,女")
        self.ban_entry.pack(fill="x", padx=16, pady=(2, 10))
        self.ban_entry.insert(0, ",".join(cfg.get("ban", ["男", "女"])))

        ctk.CTkLabel(self.side, text="封鎖字比對方式", font=(FONT, 12),
                     text_color="#8a8a9e").pack(anchor="w", padx=16)
        self.match_menu = ctk.CTkOptionMenu(self.side, values=["包含字", "完全相同"],
                                            font=(FONT, 13))
        self.match_menu.pack(fill="x", padx=16, pady=(2, 10))
        self.match_menu.set("完全相同" if cfg.get("match_mode") == "exact" else "包含字")

        ctk.CTkLabel(self.side, text="前幾句內偵測（0=不限）", font=(FONT, 12),
                     text_color="#8a8a9e").pack(anchor="w", padx=16)
        self.max_entry = ctk.CTkEntry(self.side, font=(FONT, 13), placeholder_text="3")
        self.max_entry.pack(fill="x", padx=16, pady=(2, 10))
        self.max_entry.insert(0, _fmt_num(cfg.get("max_check", 3)))

        ctk.CTkLabel(self.side, text="離開延遲上限（秒，1~x 隨機）", font=(FONT, 12),
                     text_color="#8a8a9e").pack(anchor="w", padx=16)
        self.delay_entry = ctk.CTkEntry(self.side, font=(FONT, 13), placeholder_text="5")
        self.delay_entry.pack(fill="x", padx=16, pady=(2, 10))
        self.delay_entry.insert(0, _fmt_num(cfg.get("leave_delay_max", 5)))

        ctk.CTkLabel(self.side, text="自動第一句（留空=不發）", font=(FONT, 12),
                     text_color="#8a8a9e").pack(anchor="w", padx=16)
        self.first_entry = ctk.CTkEntry(self.side, font=(FONT, 13), placeholder_text="安安你好")
        self.first_entry.pack(fill="x", padx=16, pady=(2, 10))
        self.first_entry.insert(0, cfg.get("first", "安安你好"))

        self.btn = ctk.CTkButton(self.side, text="▶ 開始配對", command=self.toggle,
                                 font=(FONT, 14, "bold"), height=40,
                                 fg_color="#4caf50", hover_color="#43a047")
        self.btn.pack(side="bottom", fill="x", padx=16, pady=16)

        # 右側：主區
        main = ctk.CTkFrame(self.root, corner_radius=0)
        main.pack(side="left", fill="both", expand=True)

        self.status = ctk.CTkLabel(main, text="⚪ 未開始", font=(FONT, 13, "bold"),
                                   text_color="#8a8a9e", anchor="w")
        self.status.pack(fill="x", padx=14, pady=(12, 4))

        self.chat = ctk.CTkTextbox(main, font=(FONT, 13), wrap="word")
        self.chat.pack(fill="both", expand=True, padx=14, pady=(4, 6))
        self.chat._textbox.tag_config("me", foreground="#8ab4f8")
        self.chat._textbox.tag_config("them", foreground="#81c995")
        self.chat._textbox.tag_config("system", foreground="#8a8a9e")
        self.chat._textbox.tag_config("match", foreground="#fdd663")
        self.chat._textbox.tag_config("leave", foreground="#f28b82")
        self.chat._textbox.tag_config("verify", foreground="#f28b82")

        inp = ctk.CTkFrame(main, corner_radius=0, fg_color="transparent")
        inp.pack(fill="x", padx=14, pady=(0, 14))
        self.entry = ctk.CTkEntry(inp, font=(FONT, 13), placeholder_text="輸入訊息，Enter 送出")
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.send())
        ctk.CTkButton(inp, text="送出", command=self.send, width=70,
                      font=(FONT, 13, "bold")).pack(side="left", padx=(8, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll)

    def append(self, kind, text):
        prefix = {"me": "[我] ", "them": "[對方] ", "system": "", "match": "",
                  "leave": "", "verify": ""}[kind]
        self.chat.configure(state="normal")
        self.chat.insert("end", prefix + text + "\n", kind)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def send(self):
        self.core.send_chat(self.entry.get())
        self.entry.delete(0, "end")

    def _read_settings(self):
        ban = [x.strip() for x in self.ban_entry.get().replace("，", ",").split(",") if x.strip()]
        try:
            max_check = int(self.max_entry.get() or "3")
        except ValueError:
            max_check = 3
        try:
            delay = float(self.delay_entry.get() or "5")
        except ValueError:
            delay = 5.0
        return {
            "ban": ban,
            "first": self.first_entry.get().strip(),
            "match_mode": "exact" if self.match_menu.get() == "完全相同" else "contains",
            "max_check": max_check,
            "leave_delay_max": delay,
        }

    def toggle(self):
        if self.core.running:
            self.core.stop()
            self.btn.configure(text="▶ 開始配對", fg_color="#4caf50", hover_color="#43a047")
        else:
            settings = self._read_settings()
            save_config(settings)
            self.core.start(settings)
            self.btn.configure(text="■ 停止", fg_color="#f44336", hover_color="#d32f2f")

    def _poll(self):
        while True:
            try:
                kind, text = self.core.log_q.get_nowait()
            except queue.Empty:
                break
            self.append(kind, text)

        if self.core.running:
            if self.core.matched:
                self.status.configure(text="🟢 已配對，可以聊天", text_color="#81c995")
            else:
                self.status.configure(text="🟡 配對中…", text_color="#fdd663")
            if self.btn.cget("text") != "■ 停止":
                self.btn.configure(text="■ 停止", fg_color="#f44336", hover_color="#d32f2f")
        else:
            if self.btn.cget("text") != "▶ 開始配對":
                self.btn.configure(text="▶ 開始配對", fg_color="#4caf50", hover_color="#43a047")
                self.status.configure(text="🔴 已停止", text_color="#8a8a9e")
        self.root.after(100, self._poll)

    def on_close(self):
        self.core.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App(WooCore()).run()
