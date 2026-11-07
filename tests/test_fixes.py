"""WooTalkBot P0(4) + P1(8) 修復的對應測試。

全部使用假 websocket / 假 session，**不會連線 wootalk.today**
（fresh_session、_http_check、websockets.connect 都被替換）。
跑法： xvfb-run -a .venv/bin/python -m pytest tests/ -q
"""
import asyncio
import json
import os
import sys
import threading
import time

import pytest

import wootalk_app as W

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SETTINGS = {"ban": ["男"], "first": "安安你好", "match_mode": "contains",
            "max_check": 3, "leave_delay_max": 1.0}


# --------------------------------------------------------------- 假物件
class State:
    def __init__(self, name="OPEN"):
        self.name = name


class FakeWS:
    """假 websocket：記錄送出的幀、close 是否真的被 await。"""

    def __init__(self):
        self.sent = []
        self.close_calls = 0
        self.state = State()

    async def send(self, raw):
        if self.state.name != "OPEN":
            raise ConnectionError("Connection is closed")
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        self.close_calls += 1
        self.state.name = "CLOSED"


class ScriptConn:
    """給 _round() 用的假連線：照腳本吐 frame，close 後結束迭代。"""

    made = 0
    live = 0

    def __init__(self, frames=()):
        self.q = asyncio.Queue()
        for f in frames:
            self.q.put_nowait(f)
        self.sent = []
        self.close_calls = 0
        self.state = State()
        ScriptConn.made += 1
        ScriptConn.live += 1

    async def send(self, raw):
        if self.state.name != "OPEN":
            raise ConnectionError("Connection is closed")
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        self.close_calls += 1
        if self.state.name == "OPEN":
            self.state.name = "CLOSED"
            ScriptConn.live -= 1
        self.q.put_nowait(None)           # 模擬真 ws：close 後 async for 會結束

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.close()
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.q.get()
        if item is None:
            raise StopAsyncIteration
        return item


def core(**kw):
    c = W.WooCore()
    c.ws = FakeWS()
    c.first = "安安你好"
    c.leave_delay_max = 1.0
    c.running = True
    c.matched = True
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def frames(ws, name):
    return [f for f in ws.sent if f[0] == name]


def logs(c):
    out = []
    while not c.log_q.empty():
        out.append(c.log_q.get_nowait())
    return out


# ------------------------------------------------- P0-1 例外寫 log
def test_p0_1_excepthook_writes_traceback_with_version(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "_log_dirs", lambda: [str(tmp_path)])
    W.install_excepthook()
    try:
        raise ValueError("boom-測試")
    except ValueError:
        W._sys_excepthook(*sys.exc_info())
    text = (tmp_path / "wootalk_error.log").read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "ValueError: boom-測試" in text
    assert f"v{W.APP_VERSION}" in text.splitlines()[0]


def test_p0_1_thread_excepthook_writes_log(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "_log_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(threading, "excepthook", W._thread_excepthook)

    def boom():
        raise RuntimeError("thread-boom")

    t = threading.Thread(target=boom)
    t.start()
    t.join()
    text = (tmp_path / "wootalk_error.log").read_text(encoding="utf-8")
    assert "RuntimeError: thread-boom" in text


# ------------------------------------------------- P0-2 stop() 真的停
def test_p0_2_stop_joins_thread_and_no_double_connection(monkeypatch):
    ScriptConn.made = ScriptConn.live = 0
    monkeypatch.setattr(W, "fresh_session", lambda: "FAKE_SESSION")
    monkeypatch.setattr(W.WooCore, "_http_check", lambda self: "up")
    monkeypatch.setattr(W.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(W.websockets, "connect", lambda *a, **k: ScriptConn())

    c = W.WooCore()
    c.start(SETTINGS)
    time.sleep(0.4)
    assert ScriptConn.live == 1
    old = c.thread

    t0 = time.monotonic()
    c.stop()
    elapsed = time.monotonic() - t0
    assert not old.is_alive(), "stop() 後 thread 還活著"
    assert elapsed < 0.5, f"stop() 花了 {elapsed:.2f}s"

    c.start(SETTINGS)          # 停止→立刻開始
    time.sleep(0.4)
    assert ScriptConn.live == 1, f"同時存活的連線數 = {ScriptConn.live}"
    c.stop()


# ------------------------------------------------- P0-3 backoff + 清 cookie
def test_p0_3_backoff_grows_and_caps_at_60(monkeypatch):
    monkeypatch.setattr(W.random, "uniform", lambda a, b: 0.0)
    c = W.WooCore()
    seq = []
    for fails in range(1, 9):
        c.fails = fails
        seq.append(c._backoff_delay())
    assert seq == [2, 4, 8, 16, 32, 60, 60, 60], seq
    c.fails = 0
    assert c._backoff_delay() == 0.0


def test_p0_3_connection_error_clears_session(monkeypatch):
    c = W.WooCore()
    c.session = "STALE_COOKIE"
    c.running = True

    async def boom():
        raise ConnectionRefusedError("cookie 失效被拒")

    naps = []

    async def fake_nap(seconds):
        naps.append(seconds)
        c.running = False

    c._round = boom
    c._nap = fake_nap
    asyncio.run(c._main())
    assert c.session is None, "連線類例外後沒有清 session（下輪不會重取 cookie）"
    assert c.fails == 1
    assert naps and naps[0] >= 2, naps


# ------------------------------------------------- P0-4 tgbot.py 移出 repo
def test_p0_4_tgbot_moved_out_of_repo_and_ignored():
    assert not os.path.exists(os.path.join(REPO, "tgbot.py"))
    lab = os.path.join(os.path.expanduser("~"), "wootalk-lab", "tgbot.py")
    assert os.path.exists(lab), "tgbot.py 不在 ~/wootalk-lab/"
    ignored = open(os.path.join(REPO, ".gitignore"), encoding="utf-8").read().split()
    assert "tgbot.py" in ignored


# ------------------------------------------------- P1-5 空事件防呆
def test_p1_5_handle_empty_event_does_not_raise():
    c = core()
    for ev in ([], (), None, ""):
        asyncio.run(c._handle(ev))
    assert c.ws.sent == []


def test_p1_5_round_ignores_empty_batch(monkeypatch):
    monkeypatch.setattr(W, "fresh_session", lambda: "FAKE_SESSION")
    conn = ScriptConn(["[]", json.dumps([["websocket_rails.ping", {}]]), None])
    monkeypatch.setattr(W.websockets, "connect", lambda *a, **k: conn)
    c = W.WooCore()
    c.session = "FAKE_SESSION"
    c.running = True
    asyncio.run(c._round())
    assert [f[0] for f in conn.sent] == ["websocket_rails.pong"]


# ------------------------------------------------- P1-6 botcheck / 未知 status
def test_p1_6_botcheck_status_detected_without_keyword(monkeypatch):
    monkeypatch.setattr(W.webbrowser, "open", lambda url: True)
    c = core()
    ws = c.ws
    asyncio.run(c._handle(["new_message", {"data": {
        "sender": 0, "status": "chat_botcheck",
        "message": "請完成驗證 https://wootalk.today/verify/abc-123"}}]))
    kinds = [k for k, _ in logs(c)]
    assert c.running is False
    assert ws.close_calls == 1
    assert "verify" in kinds


def test_p1_6_unknown_status_logged(monkeypatch):
    c = core()
    asyncio.run(c._handle(["new_message", {"data": {
        "sender": 0, "status": "chat_waiting", "message": ""}}]))
    entries = logs(c)
    assert any(k == "system" and "[狀態] chat_waiting" in t for k, t in entries), entries


# ------------------------------------------------- P1-7 _spawn 收例外
def test_p1_7_spawn_writes_exception_to_gui_log():
    c = core()

    async def boom():
        raise ValueError("孤兒 task 例外")

    async def run():
        c._spawn(boom())
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert any(k == "system" and "ValueError" in t for k, t in logs(c))


# ------------------------------------------------- P1-8 併發離開
def test_p1_8_concurrent_leave_no_exception(monkeypatch):
    monkeypatch.setattr(W.random, "uniform", lambda a, b: 0.1)   # 延遲壓成 0.1s
    c = core(matched=True)
    ws = c.ws

    async def run():
        task = asyncio.ensure_future(c._handle(["new_message", {"data": {
            "sender": 0, "status": "chat_otherleave", "message": ""}}]))
        await asyncio.sleep(0.03)        # 還在 0.1s 延遲中
        c._user_leave()                  # 使用者這時按「離開」
        await asyncio.sleep(0.25)        # 讓兩條路徑都跑完
        await task

    asyncio.run(run())
    assert len(frames(ws, "change_person")) == 1
    assert ws.close_calls == 1
    assert not any("⚠️" in t for k, t in logs(c)), logs(c)


# ------------------------------------------------- P1-9 max_check=0 = 不限
def test_p1_9_max_check_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(W.random, "uniform", lambda a, b: 0.0)
    c = core(max_check=0, their_msgs=5)      # 已經是第 6 句
    ws = c.ws

    async def run():
        await c._handle(["new_message", {"data": {"sender": 2, "message": "我是男生"}}])
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert len(frames(ws, "change_person")) == 1


def test_p1_9_max_check_limit_still_respected(monkeypatch):
    monkeypatch.setattr(W.random, "uniform", lambda a, b: 0.0)
    c = core(max_check=3, their_msgs=3)      # 第 4 句
    ws = c.ws

    async def run():
        await c._handle(["new_message", {"data": {"sender": 2, "message": "我是男生"}}])
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert frames(ws, "change_person") == []


# ------------------------------------------------- P1-10 _hit 空關鍵字
def test_p1_10_hit_empty_keyword_is_false():
    c = W.WooCore()
    assert c._hit("任何字", "") is False
    c.match_mode = "exact"
    assert c._hit("", "") is False


# ------------------------------------------------- P1-11 送出成功才顯示
def test_p1_11_message_logged_only_after_send_success():
    c = core()
    asyncio.run(c._do_send("安安"))
    assert ("me", "安安") in logs(c)
    assert [f[1]["data"]["message"] for f in frames(c.ws, "new_message")] == ["安安"]


def test_p1_11_failed_send_not_displayed_as_sent():
    c = core()
    c.ws.state.name = "CLOSED"
    asyncio.run(c._do_send("安安"))
    entry = logs(c)
    assert not any(k == "me" for k, _ in entry)
    assert any(k == "system" and "送出失敗" in t for k, t in entry)


def test_p1_11_send_chat_does_not_optimistically_log():
    c = core()
    c.loop = None
    c.send_chat("安安")
    assert logs(c) == []


# ------------------------------------------------- P1-12 sleep 不阻塞事件流
def test_p1_12_match_delay_does_not_block_handle(monkeypatch):
    monkeypatch.setattr(W.random, "uniform", lambda a, b: 2.0)   # 第一句延遲 2 秒
    c = core(first="安安你好", matched=False)

    async def run():
        t0 = time.monotonic()
        await c._handle(["new_message", {"data": {
            "sender": 0, "status": "chat_started", "message": ""}}])
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0.1)
        return elapsed

    elapsed = asyncio.run(run())
    assert elapsed < 0.5, f"_handle 被 sleep 卡住 {elapsed:.2f}s"
    assert frames(c.ws, "new_message") == []


def test_p1_12_first_message_sent_after_delay(monkeypatch):
    monkeypatch.setattr(W.random, "uniform", lambda a, b: 0.05)
    c = core(first="安安你好", matched=False)

    async def run():
        await c._handle(["new_message", {"data": {
            "sender": 0, "status": "chat_started", "message": ""}}])
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert [f[1]["data"]["message"] for f in frames(c.ws, "new_message")] == ["安安你好"]
    assert ("me", "安安你好") in logs(c)


def test_p1_12_first_message_skipped_when_already_left(monkeypatch):
    monkeypatch.setattr(W.random, "uniform", lambda a, b: 0.1)
    c = core(first="安安你好", matched=False)

    async def run():
        await c._handle(["new_message", {"data": {
            "sender": 0, "status": "chat_started", "message": ""}}])
        c.matched = False                 # 延遲期間對方離開
        await asyncio.sleep(0.3)

    asyncio.run(run())
    assert frames(c.ws, "new_message") == []


# ------------------------------------------------- 回歸測試
def test_send_raises_when_socket_closed():
    c = core()
    c.ws.state.name = "CLOSED"
    with pytest.raises(ConnectionError):
        asyncio.run(c._send("websocket_rails.pong"))


def test_ping_gets_pong_on_open_socket():
    c = core()
    asyncio.run(c._handle(["websocket_rails.ping", {}]))
    assert frames(c.ws, "websocket_rails.pong") == [["websocket_rails.pong", {}]]
