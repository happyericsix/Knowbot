import sqlite3

from knowbot.envelope import Event
from knowbot.store.db import SCHEMA_VERSION, EventStore, iter_events, sensor_stats, tail_events


def test_store_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = EventStore(db, batch_size=2, flush_interval=0.05)
    for i in range(5):
        store.append(Event("t.s", "t", {"i": i, "备注": "中文键值"}))
    store.close()

    events = list(iter_events(db))
    assert [e["payload"]["i"] for e in events] == list(range(5))
    assert events[0]["payload"]["备注"] == "中文键值"

    # 批量阈值触发过（batch_size=2，5 条 > 2）且 close 冲刷余量
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM event_raw").fetchone()[0] == 5
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_tail_newest_first(tmp_path):
    db = tmp_path / "t.db"
    store = EventStore(db, batch_size=100)
    for i in range(3):
        store.append(Event("t.s", "t", {"i": i}))
    store.close()

    events = tail_events(db, 2)
    assert [e["payload"]["i"] for e in events] == [2, 1]  # 最新在前


def test_stats_shape(tmp_path):
    db = tmp_path / "t.db"
    store = EventStore(db)
    for sensor in ("a.b", "a.b", "c.d"):
        store.append(Event(sensor, "t", {}))
    store.close()

    stats = sensor_stats(db)
    assert stats["total"] == 3
    assert stats["user_version"] == SCHEMA_VERSION
    assert stats["sensors"]["a.b"]["count"] == 2
    assert stats["sensors"]["c.d"]["count"] == 1


def test_reopen_appends(tmp_path):
    db = tmp_path / "t.db"
    store = EventStore(db)
    store.append(Event("t.s", "t", {}))
    store.close()
    store = EventStore(db)  # 重开：schema 幂等
    store.append(Event("t.s", "t2", {}))
    store.close()
    assert len(list(iter_events(db))) == 2
