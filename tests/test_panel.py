"""③ 面板集成测试：真 HTTP 服务（随机端口）+ 临时库 + Fake 执行器。"""
import json
import sqlite3
import urllib.error
import urllib.request

import pytest

from knowbot.panel.server import PanelServer
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore, seed_defaults


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def get(self, device_id):
        return 0.5

    def set(self, device_id, value):
        self.calls.append((device_id, value))
        return 0.5


@pytest.fixture()
def env(tmp_path):
    from knowbot.envelope import Event
    store = EventStore(tmp_path / "p.db", batch_size=10_000, flush_interval=0.05)
    store.append(Event("system.heartbeat", "beat", {}))
    store._flush()
    memory = MemoryStore(store._conn)
    seed_defaults(memory)
    # 一条草稿信念 + 一条真执行台账
    draft = memory.get_or_create("audio.volume", "instance", "audio.device:{X}",
                                 {"scalar": 0.55}, 0.35, "observed", state="draft")
    action_id = memory.log_action(
        "volume", None, draft["id"],
        {"type": "set_volume", "device_id": "{X}", "before": 0.8, "after": 0.55},
        "notify", 1.0, undo_payload={"restore": 0.8}, latency_ms=42.0)
    dry_id = memory.log_action(
        "volume", None, draft["id"],
        {"type": "set_volume", "device_id": "{X}", "before": 0.8, "after": 0.35},
        "dry_run", 2.0, undo_payload={"restore": 0.8})
    prearm_id = memory.log_action(  # 反射层布防（台账不可豁免）
        "prearm", None, None,
        {"type": "prearm", "device_id": "{X}", "before": 0.6, "after": 0.2},
        "silent", 3.0, risk="zero", undo_payload={"restore": 0.6})
    server = PanelServer(tmp_path / "p.db", executor=FakeExecutor(), port=0,
                         flags={"dry_run": True})
    server.start()
    yield server, memory, draft["id"], action_id, dry_id, store, prearm_id
    server.stop()
    store.close()


def _req(base, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=5) as resp:
        return json.loads(resp.read().decode())


def test_overview_and_static(env):
    server, *_ = env
    base = f"http://127.0.0.1:{server.bound_port}"
    d = _req(base, "/api/overview")
    assert d["events_total"] >= 1
    assert d["preferences"].get("draft") == 1
    assert d["paused"] is False and d["flags"]["dry_run"] is True
    html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
    assert "记忆面板" in html


def test_confirm_draft(env):
    server, memory, draft_id, *_ = env
    base = f"http://127.0.0.1:{server.bound_port}"
    d = _req(base, f"/api/preference/{draft_id}/confirm", "POST")
    assert d["ok"]
    assert d["preference"]["state"] == "active"
    assert d["preference"]["confidence"] >= 0.9
    assert d["preference"]["source"] == "explicit"


def test_reject_draft_suppresses(env):
    server, memory, draft_id, *_ = env
    base = f"http://127.0.0.1:{server.bound_port}"
    d = _req(base, f"/api/preference/{draft_id}/reject", "POST")
    assert d["ok"] and d["suppressed"]
    row = memory.conn.execute("SELECT * FROM exception WHERE pref_key='audio.volume'").fetchone()
    assert row is not None                      # 例外表一票否决生效
    assert memory.get("audio.volume", "instance", "audio.device:{X}")["state"] == "archived"


def test_undo_restores_volume(env):
    server, memory, draft_id, action_id, dry_id, _, _ = env
    base = f"http://127.0.0.1:{server.bound_port}"
    executor = server.executor
    d = _req(base, f"/api/action/{action_id}/undo", "POST")
    assert d["ok"] and d["restored"] == 0.8
    assert executor.calls == [("{X}", 0.8)]     # 真的调了执行器恢复
    row = memory.conn.execute(
        "SELECT reaction, quadrant FROM action_log WHERE id=?", (action_id,)).fetchone()
    assert row["reaction"] == "reject"          # 撤销=事后显式拒绝
    undo_row = memory.conn.execute(
        "SELECT action FROM action_log WHERE action LIKE '%undo%'").fetchone()
    assert undo_row is not None                 # 撤销本身也进台账
    # 影子动作无实际效果 → 拒绝撤销
    d2 = _req(base, f"/api/action/{dry_id}/undo", "POST")
    assert d2["ok"] is False


def test_undo_prearm_restores(env):
    """反射层 prearm 布防同样可撤销（恢复到布防前音量）。"""
    server, memory, *_ , prearm_id = env
    base = f"http://127.0.0.1:{server.bound_port}"
    executor = server.executor
    d = _req(base, f"/api/action/{prearm_id}/undo", "POST")
    assert d["ok"] and d["restored"] == 0.6
    assert executor.calls[-1] == ("{X}", 0.6)
    row = memory.conn.execute(
        "SELECT reaction, quadrant FROM action_log WHERE id=?", (prearm_id,)).fetchone()
    assert row["reaction"] == "reject"          # 撤销即裁决：记误报


def test_pause_toggle(env):
    server, *_ = env
    base = f"http://127.0.0.1:{server.bound_port}"
    d = _req(base, "/api/pause", "POST", {"on": True})
    assert d["paused"] is True
    assert env[1].get_meta("paused") == "1"     # 技能侧可见（出手中止）
    d = _req(base, "/api/pause", "POST", {"on": False})
    assert d["paused"] is False


def test_calibrate_endpoint(env):
    """面板三键校准：与 toast 回传同一学习器路径（draft → ok → active/0.9）。"""
    server, memory, draft_id, *_ = env
    base = f"http://127.0.0.1:{server.bound_port}"
    d = _req(base, "/api/calibrate", "POST", {"pref_id": draft_id, "value": "ok"})
    assert d["ok"] and d["pref_id"] == draft_id and not d["created_instance"]
    row = memory.get_pref(draft_id)
    assert row["state"] == "active" and row["confidence"] >= 0.9
    n = memory.conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE pref_id=? AND kind='calibration'",
        (draft_id,)).fetchone()[0]
    assert n == 1
    # 非法值 400
    try:
        _req(base, "/api/calibrate", "POST", {"pref_id": draft_id, "value": "hi"})
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_overview_quadrant_week(env):
    """周窗四象限 + FA 率（影子周健康线读数）。"""
    server, memory, draft_id, *_ = env
    base = f"http://127.0.0.1:{server.bound_port}"
    # env 里那条 notify 台账未裁决 → unresolved；补一条回填 false_alarm
    memory.finish_action(env[3], "correct", "false_alarm", {"rule": "A1"})
    d = _req(base, "/api/overview")
    qw = d["quadrant_week"]
    assert qw["window_days"] == 7 and qw["health_line"] == 0.10
    assert qw["counts"]["false_alarm"] == 1
    assert qw["fa_rate"] == 1.0                      # 唯一出手即误报
    # dry_run 那条待裁决=1；prearm 布防（无观察窗）不计入待裁决
    assert qw["unresolved"] == 1
