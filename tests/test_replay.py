from knowbot.replay.player import is_session_boundary, load_file, play
from knowbot.replay.record import dump_jsonl


def make_events():
    return [
        {"seq": 1, "ts_mono": 10.0, "ts_wall": "w1", "sensor": "a.b", "type": "x", "payload": {}},
        {"seq": 2, "ts_mono": 10.5, "ts_wall": "w2", "sensor": "a.b", "type": "y", "payload": {}},
        # 睡眠唤醒：mono 回绕，属会话边界
        {"seq": 3, "ts_mono": 0.2, "ts_wall": "w3", "sensor": "system.clock_reconcile",
         "type": "reconcile", "payload": {}},
        {"seq": 4, "ts_mono": 0.3, "ts_wall": "w4", "sensor": "a.b", "type": "x", "payload": {}},
    ]


def test_play_sessions_and_no_false_violation():
    got = []
    stats = play(make_events(), got.append)
    assert stats["events"] == 4
    assert stats["sessions"] == 2
    # 跨会话的 mono 回绕（10.5 → 0.2 → 0.3）不计违规
    assert stats["mono_violations"] == 0
    assert stats["seq_violations"] == 0
    assert len(got) == 4


def test_play_detects_in_session_violation():
    events = make_events()[:2]
    events[1]["ts_mono"] = 9.9  # 会话内倒序
    stats = play(events, handler=lambda ev: None)
    assert stats["mono_violations"] == 1


def test_daemon_stop_is_boundary():
    events = [
        {"seq": 1, "ts_mono": 5.0, "ts_wall": "w1", "sensor": "a.b", "type": "x", "payload": {}},
        {"seq": 2, "ts_mono": 5.1, "ts_wall": "w2", "sensor": "system.daemon", "type": "stop", "payload": {}},
        # 进程重启：seq 与 mono 均回绕
        {"seq": 1, "ts_mono": 0.1, "ts_wall": "w3", "sensor": "system.daemon", "type": "start", "payload": {}},
        {"seq": 2, "ts_mono": 0.2, "ts_wall": "w4", "sensor": "a.b", "type": "x", "payload": {}},
    ]
    stats = play(events, handler=lambda ev: None)
    assert stats["sessions"] == 2
    assert stats["mono_violations"] == 0
    assert stats["seq_violations"] == 0


def test_boundary_predicate():
    assert is_session_boundary({"sensor": "system.clock_reconcile", "type": "reconcile"})
    assert is_session_boundary({"sensor": "system.daemon", "type": "stop"})
    assert not is_session_boundary({"sensor": "system.daemon", "type": "start"})


def test_dump_load_roundtrip(tmp_path):
    f = tmp_path / "golden.jsonl"
    events = make_events()
    n = dump_jsonl(events, f)
    assert n == 4
    assert load_file(f) == events
