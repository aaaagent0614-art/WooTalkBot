#!/usr/bin/env python3
"""WooTalk 自動化 — Windows 桌面版。

單一檔案，Python 標準庫(Tkinter) + websockets 即可跑：
    pip install websockets
    python wootalk_app.py

打包成免安裝 exe：
    pip install pyinstaller
    pyinstaller -F -w -n WooTalkBot wootalk_app.py
"""
import asyncio
import http.cookiejar
import json
import queue
import random
import threading
import tkinter as tk
import urllib.request
from tkinter import scrolledtext, messagebox

import websockets

WSS = "wss://wootalk.today/websocket"
HOME = "https://wootalk.today/"


def fresh_session():
    """先載入首頁拿 _wootalk_session cookie(WebSocket 握手必需)。"""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
    op.open(HOME)
    for c in cj:
        if c.name == "_wootalk_session":
            return c.value
    return None


class WooCore:
    """wootalk 連線邏輯，跑在獨立 thread 的 asyncio loop 裡，
    事件透過 self.log_q 拋回 GUI。"""

    def __init__(self):
        self.log_q = queue.Queue()
        self.loop = None
        self.thread = None
        self.ws = None
        self.running = False
        self.ban = ["男", "女"]
        self.first = "安安你好"
        self.max_check = 3
        self.round = 0
        self.their_msgs = 0
        self.sent_first = False

    # ---- GUI 端控制 ----
    def start(self, ban, first):
        if self.running:
            return
        self.ban = ban
        self.first = first
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

    def log(self, text):
        self.log_q.put(text)

    def _rid(self):
        return random.randint(100000, 999999)

    async def _send(self, name, data=None):
        frame = [name, {"id": self._rid(), "data": data}] if data is not None else [name, {}]
        await self.ws.send(json.dumps(frame, ensure_ascii=False))

    async def _say(self, text):
        await self._send("new_message", {"message": text, "msg_id": self._rid()})

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
                    if status == "chat_started" and not self.sent_first:
                        self.sent_first = True
                        self.log("✅ 配對成功")
                        await asyncio.sleep(0.4)
                        if self.first:
                            await self._say(self.first)
                            self.log(f"📤 已自動發：{self.first}")
                    elif status == "chat_otherleave":
                        self.log("👋 對方離開，換下一個")
                        await self._send("change_person")
                        await self.ws.close()
                        return
                else:  # 陌生人
                    self.their_msgs += 1
                    hit = [k for k in self.ban if k and k in text]
                    if hit and self.their_msgs <= self.max_check:
                        self.log(f"👤 對方：{text}")
                        self.log(f"⛔ 前{self.max_check}句命中封鎖字 {hit} → 離開")
                        await self._send("change_person")
                        await self.ws.close()
                        return
                    self.log(f"👤 對方：{text}")

    async def _round(self):
        sess = fresh_session()
        if not sess:
            self.log("⚠️ 拿不到 session cookie，稍後重試")
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
            self.log(f"── 第 {self.round} 輪配對 ──")
            try:
                await self._round()
            except websockets.ConnectionClosed:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"⚠️ {type(e).__name__}: {e}")
            await asyncio.sleep(2)
        self.log("⏸ 已停止")


class App:
    def __init__(self, core: WooCore):
        self.core = core
        self.root = tk.Tk()
        self.root.title("WooTalk 自動化")
        self.root.geometry("560x520")
        self.root.resizable(True, True)

        # 封鎖字
        frm = tk.Frame(self.root)
        frm.pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(frm, text="封鎖字(逗號分隔):").pack(side="left")
        self.ban_var = tk.StringVar(value="男,女")
        tk.Entry(frm, textvariable=self.ban_var).pack(side="left", fill="x", expand=True, padx=6)

        # 首句
        frm2 = tk.Frame(self.root)
        frm2.pack(fill="x", padx=10, pady=6)
        tk.Label(frm2, text="自動第一句:").pack(side="left")
        self.first_var = tk.StringVar(value="安安你好")
        tk.Entry(frm2, textvariable=self.first_var).pack(side="left", fill="x", expand=True, padx=6)

        # 開始/停止
        self.btn = tk.Button(self.root, text="▶ 開始配對", command=self.toggle,
                             height=2, bg="#4caf50", fg="white", font=("", 12, "bold"))
        self.btn.pack(fill="x", padx=10, pady=6)

        # log
        self.log_box = scrolledtext.ScrolledText(self.root, height=18, state="disabled",
                                                 font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll)

    def toggle(self):
        if self.core.running:
            self.core.stop()
            self.btn.config(text="▶ 開始配對", bg="#4caf50")
        else:
            ban = [x.strip() for x in self.ban_var.get().replace("，", ",").split(",") if x.strip()]
            self.core.start(ban, self.first_var.get().strip())
            self.btn.config(text="■ 停止", bg="#f44336")

    def _poll(self):
        while True:
            try:
                msg = self.core.log_q.get_nowait()
            except queue.Empty:
                break
            self.log_box.config(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        # 停止後把按鈕還原
        if not self.core.running and self.btn["text"] == "■ 停止":
            self.btn.config(text="▶ 開始配對", bg="#4caf50")
        self.root.after(100, self._poll)

    def on_close(self):
        self.core.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App(WooCore()).run()
