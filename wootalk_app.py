#!/usr/bin/env python3
"""WooTalk 自動化 — Windows 桌面版（可自動配對 + 手動聊天）。

單一檔案，Python 標準庫(Tkinter) + websockets 即可跑：
    pip install websockets
    python wootalk_app.py

打包 exe：
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
from tkinter import ttk

import websockets

WSS = "wss://wootalk.today/websocket"
HOME = "https://wootalk.today/"

# Catppuccin 深色主題
C = {
    "bg": "#1e1e2e", "panel": "#181825", "field": "#313244",
    "fg": "#cdd6f4", "muted": "#6c7086",
    "me": "#89b4fa", "them": "#a6e3a1", "system": "#6c7086",
    "match": "#f9e2af", "leave": "#f38ba8", "accent": "#4caf50",
    "accent_off": "#f44336",
}


def fresh_session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
    op.open(HOME)
    for c in cj:
        if c.name == "_wootalk_session":
            return c.value
    return None


class WooCore:
    """wootalk 連線邏輯，跑在獨立 thread 的 asyncio loop。
    事件用 (kind, text) 拋回 GUI；kind ∈ system/me/them/match/leave。"""

    def __init__(self):
        self.log_q = queue.Queue()
        self.loop = None
        self.thread = None
        self.ws = None
        self.running = False
        self.matched = False
        self.ban = ["男", "女"]
        self.first = "安安你好"
        self.max_check = 3
        self.round = 0
        self.their_msgs = 0
        self.sent_first = False
        self.msg_id = 0

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

    async def _send(self, name, data=None):
        frame = [name, {"id": self._rid(), "data": data}] if data is not None else [name, {}]
        await self.ws.send(json.dumps(frame, ensure_ascii=False))

    async def _say(self, text):
        self.msg_id += 1
        await self._send("new_message", {"message": text, "msg_id": self.msg_id})

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
                        self.matched = True
                        self.log(("match", "✅ 已配對，可以開始聊天"))
                        await asyncio.sleep(0.4)
                        if self.first:
                            await self._say(self.first)
                            self.log(("me", self.first))
                    elif status == "chat_otherleave":
                        self.matched = False
                        self.log(("leave", "👋 對方離開，換下一個"))
                        await self._send("change_person")
                        await self.ws.close()
                        return
                else:  # 陌生人
                    self.their_msgs += 1
                    hit = [k for k in self.ban if k and k in text]
                    if hit and self.their_msgs <= self.max_check:
                        self.log(("them", text))
                        self.log(("leave", f"⛔ 前{self.max_check}句命中封鎖字 {hit} → 自動離開"))
                        self.matched = False
                        await self._send("change_person")
                        await self.ws.close()
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
        self.root = tk.Tk()
        self.root.title("WooTalk")
        self.root.geometry("600x640")
        self.root.configure(bg=C["bg"])

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # 狀態列
        self.status = tk.Label(self.root, text="⚪ 未開始", bg=C["bg"], fg=C["muted"],
                               font=("Segoe UI", 11, "bold"), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(10, 0))

        # 對話區
        self.chat = tk.Text(self.root, bg=C["panel"], fg=C["fg"], bd=0, relief="flat",
                            font=("Segoe UI", 11), wrap="word", state="disabled",
                            padx=10, pady=8)
        self.chat.tag_configure("me", foreground=C["me"])
        self.chat.tag_configure("them", foreground=C["them"])
        self.chat.tag_configure("system", foreground=C["system"])
        self.chat.tag_configure("match", foreground=C["match"])
        self.chat.tag_configure("leave", foreground=C["leave"])
        self.chat.pack(fill="both", expand=True, padx=12, pady=(6, 6))

        # 輸入列
        inp = tk.Frame(self.root, bg=C["bg"])
        inp.pack(fill="x", padx=12)
        self.entry = tk.Entry(inp, bg=C["field"], fg=C["fg"], insertbackground=C["fg"],
                              relief="flat", font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.entry.bind("<Return>", lambda e: self.send())
        tk.Button(inp, text="送出", command=self.send, bg=C["me"], fg="#11111b",
                  relief="flat", font=("Segoe UI", 10, "bold"), padx=16, pady=7,
                  activebackground=C["me"]).pack(side="left", padx=(8, 0))

        # 設定列
        cfg = tk.Frame(self.root, bg=C["bg"])
        cfg.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(cfg, text="封鎖字", bg=C["bg"], fg=C["muted"], font=("Segoe UI", 9)).pack(side="left")
        self.ban_var = tk.StringVar(value="男,女")
        tk.Entry(cfg, textvariable=self.ban_var, width=14, bg=C["field"], fg=C["fg"],
                 insertbackground=C["fg"], relief="flat").pack(side="left", padx=(4, 12))
        tk.Label(cfg, text="首句", bg=C["bg"], fg=C["muted"], font=("Segoe UI", 9)).pack(side="left")
        self.first_var = tk.StringVar(value="安安你好")
        tk.Entry(cfg, textvariable=self.first_var, bg=C["field"], fg=C["fg"],
                 insertbackground=C["fg"], relief="flat").pack(side="left", fill="x", expand=True, padx=(4, 0))

        # 開始/停止
        self.btn = tk.Button(self.root, text="▶ 開始配對", command=self.toggle,
                             bg=C["accent"], fg="#11111b", relief="flat",
                             font=("Segoe UI", 12, "bold"), pady=8,
                             activebackground=C["accent"])
        self.btn.pack(fill="x", padx=12, pady=(8, 12))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll)

    def append(self, kind, text):
        prefix = {"me": "[我] ", "them": "[對方] ", "system": "", "match": "", "leave": ""}[kind]
        self.chat.config(state="normal")
        self.chat.insert("end", prefix + text + "\n", kind)
        self.chat.see("end")
        self.chat.config(state="disabled")

    def send(self):
        self.core.send_chat(self.entry.get())
        self.entry.delete(0, "end")

    def toggle(self):
        if self.core.running:
            self.core.stop()
            self.btn.config(text="▶ 開始配對", bg=C["accent"])
        else:
            ban = [x.strip() for x in self.ban_var.get().replace("，", ",").split(",") if x.strip()]
            self.core.start(ban, self.first_var.get().strip())
            self.btn.config(text="■ 停止", bg=C["accent_off"])

    def _poll(self):
        while True:
            try:
                kind, text = self.core.log_q.get_nowait()
            except queue.Empty:
                break
            self.append(kind, text)

        if self.core.running:
            if self.core.matched:
                self.status.config(text="🟢 已配對，可以聊天", fg=C["them"])
            else:
                self.status.config(text="🟡 配對中…", fg=C["match"])
            if self.btn["text"] != "■ 停止":
                self.btn.config(text="■ 停止", bg=C["accent_off"])
        else:
            if self.btn["text"] != "▶ 開始配對":
                self.btn.config(text="▶ 開始配對", bg=C["accent"])
                self.status.config(text="🔴 已停止", fg=C["muted"])
        self.root.after(100, self._poll)

    def on_close(self):
        self.core.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App(WooCore()).run()
