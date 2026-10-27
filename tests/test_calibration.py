"""三键校准闭环测试：语义黄金（learner）+ URI 解析 + toast 按钮 XML + 溯源反查。

语义依据：docs/phase1-memory-ddl.md §7（刚好→实例信念诞生 explicit/0.9/active）
与 docs/design-amendments.md §12（太轻/太吵→轻罚不动值）。
"""
import json

from knowbot.envelope import Event
from knowbot.learning.learner import VolumeLearner
from knowbot.notify.protocol import calibrate_uri, parse_calibrate_uri
from knowbot.notify.toast import _build_xml
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore, seed_defaults

DEV = "{calib-device}"
INST = f"audio.device:{DEV}"
CLS = "audio.form_factor:headphones"


def _setup(tmp_path):
    store = EventStore(tmp_path / "c.db", batch_size=10_000, flush_interval=0.05)
    memory = MemoryStore(store._conn)
    seed_defaults(memory)
    return store, memory, VolumeLearner(memory)


def _make_action(memory, pref_id, mode="dry_run", after=0.35):
    return memory.log_action(
        "volume", None, pref_id,
        {"type": "set_volume", "device_id": DEV, "before": 0.8, "after": after},
        mode, 1.0, undo_payload={"restore": 0.8})


# ---------- 刚好：显式确认 ----------

def test_ok_births_instance_pref_from_class_action(tmp_path):
    """类级信念上出手被校准"刚好" → 实例信念诞生 + 锚点 + 台账回填 accept。"""
    store, memory, learner = _setup(tmp_path)
    cls = memory.get("audio.volume", "class", CLS)
    action_id = _make_action(memory, cls["id"], after=0.35)

    r = learner.apply_calibration("ok", cls["id"], action_id, via="toast")
    assert r["ok"] and r["created_instance"] and r["action_backfilled"]

    inst = memory.get("audio.volume", "instance", INST)
    assert inst is not None and inst["state"] == "active"
    assert inst["source"] == "explicit" and inst["confidence"] >= 0.9
    assert json.loads(inst["value"])["scalar"] == 0.35  # 校准的是这次出手的目标值

    dmap = memory.get("audio.device_map", "instance", INST)  # 锚点照记
    assert json.loads(dmap["value"])["anchors"][-1][1] == 0.35

    action = memory.get_action(action_id)                  # 台账回填
    assert action["reaction"] == "accept"
    assert action["quadrant"] == "correct_detection"

    ev = memory.conn.execute(                              # 证据挂在实例信念上
        "SELECT detail FROM evidence WHERE pref_id=? AND kind='calibration'",
        (inst["id"],)).fetchone()
    assert ev is not None and "CAL-OK" in ev["detail"]
    store.close()


def test_ok_boosts_existing_instance_without_action(tmp_path):
    """面板直接校准实例信念（无台账）→ 原地升格，不新建信念、不回填。"""
    store, memory, learner = _setup(tmp_path)
    memory.get_or_create("audio.volume", "instance", INST,
                         {"scalar": 0.55}, 0.35, "observed", state="draft")
    pref = memory.get("audio.volume", "instance", INST)

    r = learner.apply_calibration("ok", pref["id"], None, via="panel")
    assert r["ok"] and not r["created_instance"] and not r["action_backfilled"]
    assert r["pref_id"] == pref["id"]

    row = memory.get_pref(pref["id"])
    assert row["state"] == "active" and row["source"] == "explicit"
    assert row["confidence"] == 0.9                        # MAX(0.35, 0.9)
    assert json.loads(row["value"])["scalar"] == 0.55      # 值不变（信念自己的值）
    assert memory.conn.execute(
        "SELECT COUNT(*) FROM action_log").fetchone()[0] == 0
    store.close()


def test_ok_keeps_higher_confidence(tmp_path):
    """已有 0.95 置信的信念点"刚好"不被拉低。"""
    store, memory, learner = _setup(tmp_path)
    memory.get_or_create("audio.volume", "instance", INST,
                         {"scalar": 0.55}, 0.95, "explicit")
    pref = memory.get("audio.volume", "instance", INST)
    learner.apply_calibration("ok", pref["id"], None)
    assert memory.get_pref(pref["id"])["confidence"] == 0.95
    store.close()


# ---------- 太轻/太吵：方向性纠正 ----------

def test_loud_penalizes_without_value_change(tmp_path):
    store, memory, learner = _setup(tmp_path)
    memory.get_or_create("audio.volume", "instance", INST,
                         {"scalar": 0.55}, 0.75, "explicit")
    pref = memory.get("audio.volume", "instance", INST)
    action_id = _make_action(memory, pref["id"], mode="notify", after=0.55)

    r = learner.apply_calibration("loud", pref["id"], action_id)
    assert r["ok"] and not r["created_instance"]

    row = memory.get_pref(pref["id"])
    assert abs(row["confidence"] - 0.75 * 0.8) < 1e-9      # ×γ(0.8) 轻罚
    assert json.loads(row["value"])["scalar"] == 0.55      # 值不动（幅度未知）

    action = memory.get_action(action_id)
    assert action["reaction"] == "correct"
    assert action["quadrant"] == "false_alarm"
    ev = memory.conn.execute(
        "SELECT detail FROM evidence WHERE kind='calibration'").fetchone()
    assert "CAL-LOUD" in ev["detail"]
    store.close()


# ---------- 护栏 ----------

def test_dedup_resolved_action(tmp_path):
    """同一台账双击/两处入口二次校准 → 拒绝，不重复应用。"""
    store, memory, learner = _setup(tmp_path)
    cls = memory.get("audio.volume", "class", CLS)
    action_id = _make_action(memory, cls["id"])
    assert learner.apply_calibration("ok", cls["id"], action_id)["ok"]
    r2 = learner.apply_calibration("loud", cls["id"], action_id)
    assert r2["ok"] is False and "重复" in r2["error"]
    store.close()


def test_invalid_inputs(tmp_path):
    store, memory, learner = _setup(tmp_path)
    assert learner.apply_calibration("hi", 1)["ok"] is False
    assert learner.apply_calibration("ok", 99999)["ok"] is False
    assert learner.apply_calibration("ok", 1, 99999)["ok"] is False  # 台账不存在
    store.close()


# ---------- 回传链路 ----------

def test_calibrate_uri_roundtrip():
    uri = calibrate_uri("ok", 17, 42)
    assert uri == "knowbot:calibrate?value=ok&pref=17&action=42"
    assert parse_calibrate_uri(uri) == {"value": "ok", "pref": 17, "action": 42}
    assert parse_calibrate_uri(calibrate_uri("soft", 3)) == \
        {"value": "soft", "pref": 3, "action": None}
    assert parse_calibrate_uri("knowbot:calibrate?value=hi&pref=1") is None
    assert parse_calibrate_uri("http://example.com") is None
    assert parse_calibrate_uri("knowbot:calibrate?value=ok") is None  # 缺 pref


def test_toast_xml_has_protocol_buttons():
    xml = _build_xml("t", "b", [("太轻", "knowbot:calibrate?value=soft&pref=1")])
    assert 'activationType="protocol"' in xml
    assert "knowbot:calibrate?value=soft&amp;pref=1" in xml  # & 正确转义
    assert 'launch="http://127.0.0.1:7681/"' in xml          # 点本体开面板
    plain = _build_xml("t", "b")
    assert "<actions>" not in plain


# ---------- 溯源反查 ----------

def test_find_event_id_matches_device_and_time(tmp_path):
    store, memory, _ = _setup(tmp_path)
    t = 100.0
    ev = Event("audio.flow", "device_settled", {
        "device_id": DEV, "final_state": "active", "flips": 2, "churn": False,
    }, ts_mono=t)
    other = Event("audio.flow", "device_settled", {
        "device_id": "{other}", "final_state": "active", "flips": 2, "churn": False,
    }, ts_mono=t + 0.1)
    store.append(ev); store.append(other); store._flush()

    hit = memory.find_event_id("audio.flow", "device_settled", DEV, t)
    assert hit == 1                                   # 精确命中本设备
    assert memory.find_event_id("audio.flow", "device_settled", "{nope}", t) is None
    assert memory.find_event_id("audio.flow", "device_settled", DEV, t + 60,
                                tol_s=5.0) is None    # 窗口外
    store.close()
