"""WooTalkBot P2(14) 修復的對應測試。

全部不連網：websockets.connect / _fetch_session / build_opener 都被替換。
GUI 測試在 xvfb-run 下跑：xvfb-run -a .venv/bin/python -m pytest tests/ -q
"""
import asyncio
import http.cookiejar
import importlib.metadata
import os
import queue
import re
import threading
import time

import pytest

import wootalk_app as W

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAB = os.path.join(os.path.expanduser("~"), "wootalk-lab")


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------- 假物件
class State:
    def __init__(self, name="OPEN"):
        self.name = name


class Conn:
    """假的 websockets 連線：記錄送出的幀，close 後結束迭代。"""

    def __init__(self, frames=()):
        self.q = asyncio.Queue()
        for f in frames:
            self.q.put_nowait(f)
        self.q.put_nowait(None)
        self.sent = []
        self.state = State()

    async def send(self, raw):
        self.sent.append(raw)

    async def close(self, *a, **k):
        self.state.name = "CLOSED"
        self.q.put_nowait(None)

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


class FakeOpener:
    """假的 urllib opener：記錄 addheaders，open() 時種一個 session cookie。"""

    def __init__(self, processor):
        self.processor = processor
        self.addheaders = []
        self.opened = []

    def open(self, url, timeout=None):
        self.opened.append((url, timeout))
        self.processor.cookiejar.set_cookie(http.cookiejar.Cookie(
            0, "_wootalk_session", "TESTSESS", None, False, "wootalk.today", False, False,
            "/", True, False, None, False, None, None, {}))
        return self


class StubCore:
    """給 GUI 測試用的假 core：不開 thread、不連網。"""

    def __init__(self):
        self.started = []
        self.stopped = 0
        self.running = False
        self.matched = False
        self.thread = None
        self.log_q = queue.Queue()

    def start(self, settings):
        self.started.append(settings)
        self.running = True

    def stop(self):
        self.stopped += 1
        self.running = False

    def send_chat(self, text):
        pass

    def leave(self):
        pass


@pytest.fixture
def gui(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "config_path", lambda: str(tmp_path / "wootalk_config.json"))
    core = StubCore()
    app = W.App(core)
    app.root.update_idletasks()
    yield app, core
    try:
        app.root.destroy()
    except Exception:
        pass


# ------------------------------------------------- P2-1 握手/首頁 UA
def test_p2_1_ws_handshake_uses_chrome_ua(monkeypatch):
    captured = {}

    def fake_connect(*a, **k):
        captured.update(k)
        return Conn()

    monkeypatch.setattr(W.websockets, "connect", fake_connect)
    c = W.WooCore()
    c.session = "FAKE_SESSION"
    c.running = True
    asyncio.run(c._round())

    ua = captured.get("user_agent_header")
    assert ua, "沒傳 user_agent_header → websockets 會送 Python/3.11 websockets/…"
    assert ua.startswith("Mozilla/5.0") and "Chrome/" in ua, ua
    assert "Python/3.11" not in ua and "websockets/" not in ua, ua


def test_p2_1_home_get_has_full_ua_and_accept_language(monkeypatch):
    made = {}

    def fake_build(processor):
        made["op"] = FakeOpener(processor)
        return made["op"]

    monkeypatch.setattr(W.urllib.request, "build_opener", fake_build)
    assert W._fetch_session() == "TESTSESS"

    headers = dict(made["op"].addheaders)
    assert headers["User-Agent"] == W.UA
    assert "Chrome/" in headers["User-Agent"]
    assert headers["Accept-Language"].startswith("zh-TW")
    assert made["op"].opened and made["op"].opened[0][1] == 10


# ------------------------------------------------- P2-2 不再每輪打首頁
def test_p2_2_http_check_only_first_round_and_60s(monkeypatch):
    seen = []
    real_create = asyncio.create_task

    def spy(coro, *a, **k):
        frame = getattr(coro, "cr_frame", None)
        if frame is not None and "seconds" in frame.f_locals:
            seen.append(frame.f_locals["seconds"])
        return real_create(coro, *a, **k)

    monkeypatch.setattr(W.asyncio, "create_task", spy)
    monkeypatch.setattr(W.WooCore, "_http_check", lambda self: "up")
    monkeypatch.setattr(W.websockets, "connect", lambda *a, **k: Conn())

    for rnd in (1, 2, 3):
        c = W.WooCore()
        c.session = "FAKE_SESSION"
        c.running = True
        c.round = rnd
        asyncio.run(c._round())

    assert seen == [60], f"只有第一輪該建立逾時檢查（60 秒），實際 {seen}"


# ------------------------------------------------- P2-3 共用 _fetch_session
def test_p2_3_fresh_session_and_http_check_share_fetch(monkeypatch):
    monkeypatch.setattr(W, "_fetch_session", lambda timeout=10: "SESS")
    assert W.fresh_session() == "SESS"
    assert W.WooCore()._http_check() == "up"

    monkeypatch.setattr(W, "_fetch_session", lambda timeout=10: None)
    assert W.fresh_session() is None
    assert W.WooCore()._http_check() == "down"


# ------------------------------------------------- P2-4 對話框行數上限
def test_p2_4_chat_keeps_at_most_2000_lines(gui):
    app, _ = gui
    for i in range(3000):
        app.append("system", f"line {i}")
    lines = int(app.chat.index("end-1c").split(".")[0])
    assert lines <= W.CHAT_MAX_LINES, f"行數 {lines}"
    body = app.chat.get("1.0", "end-1c")
    assert "line 2999" in body and "line 0" not in body.split("\n")[0]


# ------------------------------------------------- P2-5 時間戳 + 可複製
def test_p2_5_message_has_hhmm_timestamp(gui):
    app, _ = gui
    app.root.update()
    app.append("me", "安安")
    text = app.chat.get("1.0", "end-1c")
    assert re.match(r"^\[\d{2}:\d{2}\] \[我\] 安安", text), text


def test_p2_5_can_copy_all_via_right_click_menu(gui):
    app, _ = gui
    app.root.update()
    app.append("me", "安安")
    app.append("them", "你好")
    app._copy_all()
    app.root.update()
    clip = app.root.clipboard_get()
    assert "安安" in clip and "你好" in clip, clip
    assert app.chat._textbox.bind("<Button-3>"), "沒有右鍵選單"


# ------------------------------------------------- P2-6 設定 clamp + 寫回
@pytest.mark.parametrize("raw_max,raw_delay,want_max,want_delay", [
    ("-3", "-3", "0", "1"),
    ("9999", "9999", "50", "30"),
    ("abc", "abc", "3", "5"),
])
def test_p2_6_settings_clamped_and_written_back(gui, raw_max, raw_delay, want_max, want_delay):
    app, _ = gui
    app._set_entry(app.max_entry, raw_max)
    app._set_entry(app.delay_entry, raw_delay)
    s = app._read_settings()
    assert s["max_check"] == int(want_max)
    assert W._fmt_num(s["leave_delay_max"]) == want_delay
    assert app.max_entry.get() == want_max, app.max_entry.get()
    assert app.delay_entry.get() == want_delay, app.delay_entry.get()


# ------------------------------------------------- P2-7 存檔失敗要回報
def test_p2_7_save_config_returns_false_when_unwritable(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "config_path",
                        lambda: str(tmp_path / "not_there" / "wootalk_config.json"))
    assert W.save_config({"ban": []}) is False
    monkeypatch.setattr(W, "config_path", lambda: str(tmp_path / "ok.json"))
    assert W.save_config({"ban": []}) is True


def test_p2_7_toggle_shows_save_failure_in_gui(gui, monkeypatch):
    app, core = gui
    monkeypatch.setattr(W, "save_config", lambda cfg: False)
    app.toggle()
    assert core.started, "toggle 沒有啟動配對"
    assert "設定存檔失敗" in app.chat.get("1.0", "end-1c")


# ------------------------------------------------- P2-8 關窗停連線 + join
def test_p2_8_on_close_stops_and_joins_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "config_path", lambda: str(tmp_path / "wootalk_config.json"))
    core = StubCore()
    core.thread = None
    app = W.App(core)                    # 先建 GUI（要 ~0.3s），再起 thread 才保證 join 有東西等
    core.thread = threading.Thread(target=lambda: time.sleep(0.25), daemon=True)
    core.thread.start()

    t0 = time.monotonic()
    app.on_close()
    elapsed = time.monotonic() - t0

    assert core.stopped == 1
    assert not core.thread.is_alive(), "on_close 沒 join，thread 被程序硬殺"
    assert elapsed >= 0.2, elapsed


# ------------------------------------------------- P2-9 版本號
def test_p2_9_version_in_title_and_error_log(gui, tmp_path, monkeypatch):
    app, _ = gui
    assert W.APP_VERSION == "1.3.0"
    assert app.root.title() == "WooTalk v1.3.0", app.root.title()

    monkeypatch.setattr(W, "_log_dirs", lambda: [str(tmp_path)])
    W.log_error("boom")
    first = (tmp_path / "wootalk_error.log").read_text(encoding="utf-8").splitlines()[0]
    assert "v1.3.0" in first, first


# ------------------------------------------------- P2-10 依賴鎖版本
def test_p2_10_requirements_pinned_and_importable():
    req = read("requirements.txt").split()
    assert "websockets==17.1" in req, req
    assert "customtkinter==6.0.0" in req, req
    assert importlib.metadata.version("websockets") == "17.1"
    assert importlib.metadata.version("customtkinter") == "6.0.0"
    assert "pip install -r requirements.txt" in read("README.md")


# ------------------------------------------------- P2-11 CI 冒煙測試
def test_p2_11_workflow_has_smoke_step_artifact_and_strict_release():
    yml = read(".github/workflows/build.yml")
    assert "python -m py_compile wootalk_app.py" in yml
    assert "fail_on_unmatched_files: true" in yml
    assert "fail_on_unmatched_files: false" not in yml
    assert "actions/upload-artifact@v4" in yml
    assert "if-no-files-found: error" in yml


# ------------------------------------------------- P2-12/13 檔案搬家
def test_p2_12_bot_py_moved_to_lab():
    assert not os.path.exists(os.path.join(REPO, "bot.py"))
    assert os.path.exists(os.path.join(LAB, "bot.py")), "bot.py 不在 ~/wootalk-lab/"


def test_p2_13_experiment_scripts_moved_and_ignored():
    names = ["probe.py", "probe2.py", "probe3.py", "probe_verify.py", "exp_probe.py",
             "exp_uuid.py", "smoke2.py", "smoke_ui.py", "test_core.py", "test_downsite.py",
             "test_verify.py", "wootalk_config.json"]
    for n in names:
        assert not os.path.exists(os.path.join(REPO, n)), f"{n} 還在 repo"
        assert os.path.exists(os.path.join(LAB, n)), f"{n} 不在 lab"
    ign = read(".gitignore")
    for pat in ("/probe*.py", "/exp_*.py", "/smoke*.py", "/test_*.py", "/bot.py",
                "/wootalk_config.json", "/wootalk_error.log"):
        assert pat in ign, pat


# ------------------------------------------------- P2-14 toggle 用按鈕文字
def test_p2_14_toggle_uses_button_text_not_running_flag(gui):
    app, core = gui
    core.running = False                      # 背景已停，但 _poll 還沒更新畫面
    app.btn.configure(text="■ 停止")
    app.toggle()
    assert core.stopped == 1 and not core.started, "按停止反而又開始"
    assert app.btn.cget("text") == "▶ 開始配對"
