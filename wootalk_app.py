#!/usr/bin/env python3
"""WooTalk 自動化 — Windows 桌面版（CustomTkinter + 可自動配對 + 手動聊天）。

依賴：websockets, customtkinter
    pip install -r requirements.txt
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
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
import tkinter as tk

import customtkinter as ctk

import websockets

WSS = "wss://wootalk.today/websocket"
HOME = "https://wootalk.today/"

APP_VERSION = "1.3.0"

# 一般 Chrome 的 UA：websockets 預設會送 "Python/3.11 websockets/17.1"，那是免費的機器人特徵
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CHAT_MAX_LINES = 2000          # 對話框保留行數上限（掛整晚不會無限長）

FONT = "Microsoft JhengHei UI"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ---- 錯誤紀錄：-w 打包後沒有 console/stderr，例外只能寫檔 ----
def _log_dirs():
    """wootalk_error.log 的候選目錄：程式/exe 同目錄 → %LOCALAPPDATA% → 家目錄 → 暫存目錄。"""
    if getattr(sys, "frozen", False):            # PyInstaller 打包後
        first = os.path.dirname(sys.executable)
    else:
        first = os.path.dirname(os.path.abspath(__file__))
    dirs = [first]
    local = os.environ.get("LOCALAPPDATA")       # 裝在 Program Files 時寫不進去
    if local:
        dirs.append(local)
    dirs.append(os.path.join(os.path.expanduser("~"), ".local", "share", "WooTalkBot"))
    dirs.append(tempfile.gettempdir())
    return dirs


def error_log_path():
    """第一個可寫的目錄下的 wootalk_error.log；全都不可寫則回 None。"""
    for d in _log_dirs():
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".wootalk_probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("x")
            os.remove(probe)
            return os.path.join(d, "wootalk_error.log")
        except Exception:
            continue
    return None


def log_error(entry):
    """附寫一段文字到 log；新檔的第一行固定是版本號。"""
    path = error_log_path()
    if not path:
        return False
    try:
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8") as f:
            if fresh:
                f.write(f"WooTalkBot v{APP_VERSION}\n")
            f.write(entry if entry.endswith("\n") else entry + "\n")
        return True
    except Exception:
        return False


def _log_exc(exc_type, exc, tb):
    try:
        body = "".join(traceback.format_exception(exc_type, exc, tb))
    except Exception:
        body = repr(exc)
    log_error(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {body}")


def _sys_excepthook(exc_type, exc, tb):
    _log_exc(exc_type, exc, tb)
    try:
        sys.__excepthook__(exc_type, exc, tb)
    except Exception:
        pass                                     # windowed 下 stderr 是 None


def _thread_excepthook(args):
    _log_exc(args.exc_type, args.exc_value, args.exc_traceback)


def install_excepthook():
    """把所有未捕捉例外（主執行緒 + 背景 thread）寫進 wootalk_error.log。"""
    log_error(f"== 啟動 v{APP_VERSION} {time.strftime('%Y-%m-%d %H:%M:%S')}")
    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook


def _fetch_session(timeout=10):
    """首頁 GET 拿 _wootalk_session cookie；拿不到（網站掛了/維護中）回 None。

    fresh_session() 與 _http_check() 共用這一份，改 UA/逾時只會改到一處。
    """
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA),
                     ("Accept-Language", "zh-TW,zh;q=0.9,en;q=0.8"),
                     ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")]
    try:
        op.open(HOME, timeout=timeout)
    except Exception:
        return None
    for c in cj:
        if c.name == "_wootalk_session":
            return c.value
    return None


def fresh_session():
    return _fetch_session()


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
    """寫設定檔；成功 True，唯讀資料夾等失敗回 False（GUI 要提示，不要靜默）。"""
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _clamp_int(raw, default, lo, hi):
    """輸入框字串 → int，非數字用 default，最後夾在 lo~hi。"""
    try:
        v = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _clamp_float(raw, default, lo, hi):
    """輸入框字串 → float，非數字用 default，最後夾在 lo~hi。"""
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        v = default
    return max(lo, min(float(v), hi))


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
        self.session = None
        self.fails = 0                 # 連續連線失敗次數（backoff 用）
        self.task = None               # _main 的 task，stop() 時可取消
        self.tasks = set()             # _spawn 出來的 task（例外會進 GUI log）
        self.leave_lock = asyncio.Lock()
        self._stop_ev = asyncio.Event()

    # ---- GUI 端控制 ----
    def start(self, settings: dict):
        if self.running:
            return
        self._join_thread()          # 舊 thread 先收斂，否則 stop→start 會有兩條連線
        self.ban = settings.get("ban", [])
        self.first = settings.get("first", "")
        self.match_mode = settings.get("match_mode", "contains")
        self.max_check = int(settings.get("max_check", 3))
        self.leave_delay_max = max(1.0, min(30.0, float(settings.get("leave_delay_max", 5.0))))
        self.session = None          # 在背景 thread 裡第一次拿，避免 HTTP 阻塞 GUI
        self.round = 0
        self.fails = 0
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self._stop_ev.set()          # 打斷 _nap 的等待，讓迴圈立刻看到 running=False
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._aclose(self.ws), loop)
            except Exception:
                pass
        self._join_thread()

    def _join_thread(self, fast=0.5, slow=2.5):
        """等背景 thread 收斂：先 0.5s，還在就取消 _main 再等 2.5s（合計最多 3s）。"""
        t = self.thread
        if t is None or t is threading.current_thread() or not t.is_alive():
            return
        t.join(fast)
        if not t.is_alive():
            return
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._cancel_main)
            except Exception:
                pass
        t.join(slow)

    def leave(self):
        """離開目前對話並繼續自動配對（等同網頁版「離開」）。"""
        if not self.running:
            self.log(("system", "⚠️ 還沒開始配對"))
            return
        if self.loop:
            self.loop.call_soon_threadsafe(self._user_leave)

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
        if self.loop:
            self.loop.call_soon_threadsafe(self._queue_send, text)

    # ---- asyncio 端 ----
    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.leave_lock = asyncio.Lock()          # 綁定在這個 loop 上
        self._stop_ev = asyncio.Event()
        self.task = loop.create_task(self._main())
        try:
            loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception as e:                    # 保底：thread 不要靜默死掉
            log_error(f"_run_loop: {type(e).__name__}: {e}")
        finally:
            self.task = None
            try:                                  # 收掉殘留 task，避免 loop.close 噴警告
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            if self.loop is loop:                 # 不要蓋掉新 thread 的 loop
                self.loop = None

    def _cancel_main(self):
        task = self.task
        if task is not None and not task.done():
            task.cancel()

    async def _aclose(self, ws=None):
        """真正關閉連線（websockets 的 close() 是 coroutine，一定要 await）。"""
        if ws is None:
            ws = self.ws
        try:
            if ws is not None:
                await ws.close()
        except Exception:
            pass
        finally:
            if ws is not None and ws is self.ws:
                self.ws = None

    async def _nap(self, seconds):
        """可被 stop() 立刻打斷的 sleep（不要用裸 asyncio.sleep，否則停止要等滿）。"""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_ev.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _ws_open(self):
        return getattr(getattr(self.ws, "state", None), "name", "") == "OPEN"

    def _spawn(self, coro):
        """建 task 且把例外寫進 GUI log（-w 打包後 asyncio logger 看不到）。"""
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)

        def _done(t):
            self.tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self.log(("system", f"⚠️ {type(exc).__name__}: {exc}"))

        task.add_done_callback(_done)
        return task

    def _user_leave(self):
        was_matched = self.matched
        self.matched = False
        if not self._ws_open():
            self.log(("system", "⚠️ 目前沒有連線，無法離開"))
            return
        if was_matched:
            self.log(("leave", "👋 已離開對話，自動配對下一個…"))
        else:
            self.log(("system", "⇄ 取消本次配對，重新開始…"))
        self._spawn(self._do_user_leave())

    async def _do_user_leave(self):
        async with self.leave_lock:               # 與「對方離開」的延遲路徑互斥
            if not self._ws_open():
                return
            try:
                await self._send("change_person")
            except Exception:
                pass
            await self._aclose()

    def _queue_send(self, text):
        self._spawn(self._do_send(text))

    async def _do_send(self, text):
        try:
            await self._say(text)
        except Exception as e:
            self.log(("system", f"⚠️ 送出失敗: {e}"))
        else:
            self.log(("me", text))                # 確認送出後才顯示（不要樂觀顯示）

    def log(self, item):
        self.log_q.put(item)

    def _rid(self):
        return random.randint(100000, 999999)

    def _hit(self, text, kw):
        if not kw:
            return False
        if self.match_mode == "exact":
            return text.strip() == kw.strip()
        return kw in text

    async def _send(self, name, data=None):
        if not self._ws_open():
            raise ConnectionError("尚未連線或連線已關閉")
        frame = [name, {"id": self._rid(), "data": data}] if data is not None else [name, {}]
        await self.ws.send(json.dumps(frame, ensure_ascii=False))

    async def _say(self, text):
        msg_id = self.msg_id + 1
        await self._send("new_message", {"message": text, "msg_id": msg_id})
        self.msg_id = msg_id                  # 送成功才佔用 id

    async def _send_first(self):
        """配對後 0~3 秒才發第一句（像真人先讀一下），用 task 跑不擋事件流。"""
        await self._nap(random.uniform(0, 3))
        if not self.matched or not self.running or not self._ws_open():
            return
        try:
            await self._say(self.first)
        except Exception as e:
            self.log(("system", f"⚠️ 第一句送出失敗: {e}"))
        else:
            self.log(("me", self.first))

    async def _leave_and_reconnect(self, reason):
        self.matched = False
        delay = random.uniform(1.0, self.leave_delay_max)
        self.log(("system", f"{reason}（{delay:.1f} 秒後離開）"))
        await self._nap(delay)
        async with self.leave_lock:               # 與使用者按「離開」互斥，避免雙送／爆例外
            if not self._ws_open():
                return
            try:
                await self._send("change_person")
            except Exception:
                pass
            await self._aclose()

    async def _handle(self, ev):
        if not isinstance(ev, (list, tuple)) or not ev:
            return                            # 空事件（[] 或 null）不再 IndexError
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
                    if "要繼續使用" in text or status == "chat_botcheck":
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
                        self.log(("system", "⚠️ 驗證綁定 IP，app 與瀏覽器 session 不同，驗證通常無效"))
                        self.log(("system", "建議：開 VPN 換 IP，再按「▶ 開始配對」重試"))
                        self.log(("system", "⏸ 配對已暫停"))
                        self.matched = False
                        self.running = False          # 完全停止，不再自動重連
                        await self._aclose()
                        return
                    if status == "chat_started" and not self.sent_first:
                        self.sent_first = True
                        self.matched = True
                        self.log(("match", "✅ 已配對，可以開始聊天"))
                        if self.first:
                            self._spawn(self._send_first())   # 0~3s 延遲丟 task，不卡事件流
                    elif status == "chat_otherleave":
                        self.matched = False
                        self._spawn(self._leave_and_reconnect("👋 對方離開，換下一個"))
                        return
                    elif status and status not in ("chat_started", "chat_otherleave"):
                        self.log(("system", f"[狀態] {status} {text[:30]}"))
                else:  # 陌生人
                    self.their_msgs += 1
                    hit = [k for k in self.ban if k and self._hit(text, k)]
                    scope = "不限句數" if self.max_check <= 0 else f"前{self.max_check}句"
                    if hit and (self.max_check <= 0 or self.their_msgs <= self.max_check):
                        self.log(("them", text))
                        self._spawn(self._leave_and_reconnect(f"⛔ {scope}命中封鎖字 {hit}"))
                        return
                    self.log(("them", text))

    def _http_check(self):
        """檢查 wootalk 是否正常：能拿到 _wootalk_session cookie 就代表正常。
        維護中時 wootalk 回維護頁、不發 session cookie（實測確認）。"""
        return "up" if _fetch_session() else "down"

    async def _match_timeout(self, seconds):
        """配對超過 seconds 秒仍無結果 → 檢查網站狀態。"""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if self.matched or not self.running:
            return
        status = await asyncio.to_thread(self._http_check)
        if status == "down":
            self.log(("leave", f"⚠️ wootalk 網站疑似掛了（配對超過 {seconds:.0f} 秒無回應）"))
            self.log(("system", "已停止配對。等網站恢復後再按「▶ 開始配對」"))
            self.running = False
            await self._aclose()
        else:
            self.log(("system", f"🔎 配對超過 {seconds:.0f} 秒，網站正常（可能人少），繼續等待…"))

    async def _round(self):
        sess = self.session or fresh_session()
        self.session = sess
        if not sess:
            self.log(("leave", "⚠️ wootalk 網站疑似掛了（拿不到 session cookie）"))
            self.log(("system", "已停止配對。等網站恢復後再按「▶ 開始配對」"))
            self.running = False
            return
        headers = {"Origin": HOME.rstrip("/"), "Cookie": f"_wootalk_session={sess}"}
        # Origin 只在 additional_headers 給一次（同時傳 origin= 會送兩個 Origin，
        # 一般 Chrome 不會這樣，嚴格一點的伺服器直接 400）
        async with websockets.connect(WSS, user_agent_header=UA,
                                      additional_headers=headers) as ws:
            self.ws = ws
            # 只在「按開始後的第一輪」檢查一次網站狀態（拉到 60 秒）；
            # 正常等配對常常超過 10 秒，每輪多打一次首頁等於多開一個匿名訪客。
            timeout_task = (asyncio.create_task(self._match_timeout(60))
                            if self.round == 1 else None)
            try:
                async for raw in ws:
                    if not self.running:
                        return
                    try:
                        batch = json.loads(raw)
                    except Exception:
                        continue
                    evs = (batch if isinstance(batch, list) and batch and isinstance(batch[0], list)
                           else ([batch] if batch else []))
                    for ev in evs:
                        await self._handle(ev)
            finally:
                if timeout_task is not None:
                    timeout_task.cancel()

    def _backoff_delay(self):
        """連續連線失敗 → 指數退避（上限 60s）＋ jitter；成功後回到 0~3s。"""
        if self.fails <= 0:
            return random.uniform(0, 3)
        return min(2 ** self.fails, 60) + random.uniform(0, 3)

    async def _main(self):
        try:
            while self.running:
                self.round += 1
                self.their_msgs = 0
                self.sent_first = False
                self.matched = False
                self.log(("system", f"── 第 {self.round} 輪配對 ──"))
                try:
                    await self._round()
                    self.fails = 0
                except asyncio.CancelledError:
                    raise
                except (OSError, websockets.ConnectionClosed, websockets.InvalidStatus) as e:
                    if not self.running:
                        break
                    self.fails += 1
                    self.session = None          # 連線類錯誤 → 下輪重取 cookie
                    self.log(("system", f"⚠️ {type(e).__name__}: {e}"))
                except Exception as e:
                    self.fails += 1
                    self.log(("system", f"⚠️ {type(e).__name__}: {e}"))
                if not self.running:
                    break
                await self._nap(self._backoff_delay())   # 可被 stop() 打斷
            self.matched = False
            self.log(("system", "⏸ 已停止"))
        finally:
            for t in list(self.tasks):
                t.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)


class App:
    def __init__(self, core: WooCore):
        self.core = core
        self.root = ctk.CTk()
        self.root.title(f"WooTalk v{APP_VERSION}")
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

        top = ctk.CTkFrame(main, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        self.status = ctk.CTkLabel(top, text="⚪ 未開始", font=(FONT, 13, "bold"),
                                   text_color="#8a8a9e", anchor="w")
        self.status.pack(side="left")
        self.leave_btn = ctk.CTkButton(top, text="⇄ 離開對話", width=110, height=28,
                                       font=(FONT, 12, "bold"),
                                       command=self.leave, state="disabled")
        self.leave_btn.pack(side="right")

        self.chat = ctk.CTkTextbox(main, font=(FONT, 13), wrap="word")
        self.chat.pack(fill="both", expand=True, padx=14, pady=(4, 6))
        self.chat._textbox.tag_config("me", foreground="#8ab4f8")
        self.chat._textbox.tag_config("them", foreground="#81c995")
        self.chat._textbox.tag_config("system", foreground="#8a8a9e")
        self.chat._textbox.tag_config("match", foreground="#fdd663")
        self.chat._textbox.tag_config("leave", foreground="#f28b82")
        self.chat._textbox.tag_config("verify", foreground="#f28b82")
        # 唯讀但要能複製：右鍵選單（禁用狀態的 Text 仍可用滑鼠選取）
        self._chat_menu = tk.Menu(self.root, tearoff=0)
        self._chat_menu.add_command(label="複製選取", command=self._copy_selection)
        self._chat_menu.add_command(label="複製全部", command=self._copy_all)
        self.chat._textbox.bind("<Button-3>", self._popup_chat_menu)

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
        stamp = time.strftime("[%H:%M] ")
        self.chat.configure(state="normal")
        self.chat.insert("end", stamp + prefix + text + "\n", kind)
        self._trim_chat()
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def _trim_chat(self):
        """超過 CHAT_MAX_LINES 行就刪掉最舊的（掛整晚不會無限長、捲動不會變鈍）。"""
        last = int(self.chat.index("end-1c").split(".")[0])
        excess = last - CHAT_MAX_LINES
        if excess > 0:
            self.chat.delete("1.0", f"{excess + 1}.0")

    def _popup_chat_menu(self, event):
        self._chat_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text or "")

    def _copy_selection(self):
        try:
            text = self.chat._textbox.get("sel.first", "sel.last")
        except tk.TclError:                       # 沒選取任何字
            text = ""
        self._clipboard(text)

    def _copy_all(self):
        self._clipboard(self.chat.get("1.0", "end-1c"))

    def send(self):
        self.core.send_chat(self.entry.get())
        self.entry.delete(0, "end")

    def leave(self):
        self.core.leave()

    def _read_settings(self):
        ban = [x.strip() for x in self.ban_entry.get().replace("，", ",").split(",") if x.strip()]
        max_check = _clamp_int(self.max_entry.get(), 3, 0, 50)
        delay = _clamp_float(self.delay_entry.get(), 5.0, 1.0, 30.0)
        # 把修正後的值寫回輸入框，使用者才看得到實際生效的設定
        self._set_entry(self.max_entry, str(max_check))
        self._set_entry(self.delay_entry, _fmt_num(delay))
        return {
            "ban": ban,
            "first": self.first_entry.get().strip(),
            "match_mode": "exact" if self.match_menu.get() == "完全相同" else "contains",
            "max_check": max_check,
            "leave_delay_max": delay,
        }

    @staticmethod
    def _set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, value)

    def toggle(self):
        # 用按鈕文字判斷：core.running 是背景 thread 改的，_poll 最多晚 100ms 才更新，
        # 那段空窗用 core.running 判斷會「按停止反而又開始」
        if self.btn.cget("text") == "■ 停止":
            self.core.stop()
            self.btn.configure(text="▶ 開始配對", fg_color="#4caf50", hover_color="#43a047")
            self.status.configure(text="🔴 已停止", text_color="#8a8a9e")
        else:
            settings = self._read_settings()
            if not save_config(settings):
                self.append("system", "⚠️ 設定存檔失敗（資料夾不可寫），本次仍會用目前設定")
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
                self.leave_btn.configure(state="normal")
            else:
                self.status.configure(text="🟡 配對中…", text_color="#fdd663")
                self.leave_btn.configure(state="disabled")
            if self.btn.cget("text") != "■ 停止":
                self.btn.configure(text="■ 停止", fg_color="#f44336", hover_color="#d32f2f")
        else:
            if self.btn.cget("text") != "▶ 開始配對":
                self.btn.configure(text="▶ 開始配對", fg_color="#4caf50", hover_color="#43a047")
                self.status.configure(text="🔴 已停止", text_color="#8a8a9e")
            self.leave_btn.configure(state="disabled")
        self.root.after(100, self._poll)

    def on_close(self):
        self.core.stop()
        t = self.core.thread
        if t is not None and t.is_alive():
            t.join(0.5)              # 多等 0.5s 讓 close frame 真的送出去，別讓程序硬殺 thread
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    install_excepthook()
    App(WooCore()).run()
