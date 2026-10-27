"""schema v2（Phase 1 记忆库五表）测试：迁移、约束、解析链、棘轮。

全部使用 tmp_path 临时库，绝不碰运行中 daemon 的 data/knowbot.db。
"""
import json
import sqlite3

import pytest

from knowbot.store.db import SCHEMA_VERSION, EventStore

V1_DDL = """
CREATE TABLE event_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER NOT NULL, ts_mono REAL NOT NULL,
    ts_wall TEXT NOT NULL, sensor TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

MEMORY_TABLES = ("preference", "evidence", "action_log", "proposal", "exception")


def _open_v1_db(path):
    """模拟一个 Phase 0 时代（user_version=1）的旧库。"""
    conn = sqlite3.connect(path)
    conn.executescript(V1_DDL)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO event_raw (seq, ts_mono, ts_wall, sensor, type, payload)"
                 " VALUES (1, 1.0, 'w1', 't.s', 't', '{}')")
    conn.commit()
    conn.close()


def test_migration_v1_to_v2(tmp_path):
    """旧 v1 库被新代码打开后：五表补齐、user_version=2、既有数据无损。"""
    db = tmp_path / "old.db"
    _open_v1_db(db)

    store = EventStore(db)
    store.close()

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    for table in MEMORY_TABLES:
        assert conn.execute(
            f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='{table}'"
        ).fetchone()[0] == 1, f"缺表 {table}"
    assert conn.execute("SELECT COUNT(*) FROM event_raw").fetchone()[0] == 1
    conn.close()


def test_migration_idempotent(tmp_path):
    """重复打开（幂等）不报错、不重复建表。"""
    db = tmp_path / "idem.db"
    for _ in range(3):
        store = EventStore(db)
        store.close()
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                     " AND name IN ('preference','exception')").fetchone()[0]
    assert n == 2
    conn.close()


def _db(tmp_path):
    store = EventStore(tmp_path / "mem.db")
    return store, store._conn


def test_preference_unique_scope(tmp_path):
    """同一旋钮同一作用域只允许一条信念：新证据应更新而非插新行。"""
    _, conn = _db(tmp_path)
    conn.execute("INSERT INTO preference (pref_key, scope, scope_key, value, source,"
                 " created_at, updated_at) VALUES ('audio.volume', 'global', '', '0.3',"
                 " 'community', 'w1', 'w1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO preference (pref_key, scope, scope_key, value, source,"
                     " created_at, updated_at) VALUES ('audio.volume', 'global', '', '0.4',"
                     " 'community', 'w2', 'w2')")
    # global 归一为空串后，class 层不受影响
    conn.execute("INSERT INTO preference (pref_key, scope, scope_key, value, source,"
                 " created_at, updated_at) VALUES ('audio.volume', 'class',"
                 " 'audio.form_factor:headphones', '0.45', 'community', 'w2', 'w2')")


def test_scope_resolution_chain(tmp_path):
    """解析顺序 instance > class > global；exception 一票否决。"""
    _, conn = _db(tmp_path)
    rows = [
        ("audio.volume", "instance", "audio.device:{abc}", "0.62"),  # 应命中
        ("audio.volume", "class", "audio.form_factor:headphones", "0.45"),
        ("audio.volume", "global", "", "0.30"),
    ]
    for key, scope, skey, val in rows:
        conn.execute("INSERT INTO preference (pref_key, scope, scope_key, value, confidence,"
                     " source, state, created_at, updated_at) VALUES (?,?,?,?,0.6,'community',"
                     "'active','w','w')", (key, scope, skey, val))

    def resolve(instance_key, class_key):
        return conn.execute(
            "SELECT scope_key FROM preference p WHERE p.pref_key='audio.volume'"
            " AND p.state='active' AND p.scope_key IN (?, ?, '')"
            " AND NOT EXISTS (SELECT 1 FROM exception x WHERE x.pref_key=p.pref_key"
            "  AND x.scope_key=p.scope_key)"
            " ORDER BY CASE p.scope WHEN 'instance' THEN 0 WHEN 'class' THEN 1 ELSE 2 END",
            (instance_key, class_key),
        ).fetchall()

    assert [r[0] for r in resolve("audio.device:{abc}", "audio.form_factor:headphones")] == [
        "audio.device:{abc}", "audio.form_factor:headphones", ""]

    # 实例层被抑制 → 一票否决，直接落到 class
    conn.execute("INSERT INTO exception (pref_key, scope_key, reason, created_at)"
                 " VALUES ('audio.volume', 'audio.device:{abc}', 'user_explicit', 'w')")
    assert [r[0] for r in resolve("audio.device:{abc}", "audio.form_factor:headphones")] == [
        "audio.form_factor:headphones", ""]


def test_confidence_ratchet(tmp_path):
    """不对称棘轮：确认趋近 1 越来越慢；纠正乘性下调。"""
    _, conn = _db(tmp_path)
    cur = conn.execute("INSERT INTO preference (pref_key, scope, scope_key, value, confidence,"
                       " source, state, created_at, updated_at)"
                       " VALUES ('audio.volume','class','audio.form_factor:headphones','0.45',"
                       " 0.55,'community','active','w','w')")
    pid = cur.lastrowid

    def conf():
        return conn.execute("SELECT confidence FROM preference WHERE id=?", (pid,)).fetchone()[0]

    lam, gamma = 0.3, 0.5
    # 两次确认：0.55 → 0.685 → 0.7795（涨幅递减）
    for _ in range(2):
        conn.execute("UPDATE preference SET confidence=MIN(1.0, confidence + ?*(1-confidence))"
                     " WHERE id=?", (lam, pid))
    assert abs(conf() - (0.55 + 0.3 * 0.45 + 0.3 * (1 - 0.55 - 0.3 * 0.45))) < 1e-9
    # 一次纠正：×0.5
    conn.execute("UPDATE preference SET confidence = confidence * ? WHERE id=?", (gamma, pid))
    assert abs(conf() - 0.5 * (0.55 + 0.3 * 0.45 + 0.3 * (1 - 0.55 - 0.3 * 0.45))) < 1e-9


def test_action_log_and_evidence_roundtrip(tmp_path):
    """台账-证据-信念三表关联走查（对应设计文档 §7 的缩小版）。"""
    store, conn = _db(tmp_path)
    # 事件入库拿 id（手动冲刷，保持 store 打开）
    store.append(__import__("knowbot.envelope", fromlist=["Event"]).Event(
        "audio.device", "device_added", {"device_id": "{abc}"}))
    store._flush()
    try:
        eid = conn.execute("SELECT id FROM event_raw").fetchone()[0]
        pid = conn.execute("INSERT INTO preference (pref_key, scope, scope_key, value, confidence,"
                           " source, state, created_at, updated_at) VALUES"
                           " ('audio.volume','class','audio.form_factor:headphones','0.45',0.55,"
                           "'community','active','w','w')").lastrowid
        aid = conn.execute(
            "INSERT INTO action_log (ts_wall, ts_mono, skill, trigger_event_id, pref_id, action,"
            " undo_payload, risk, exec_mode, latency_ms)"
            " VALUES ('w', 1.0, 'volume', ?, ?, ?, '{}', 'zero', 'notify', 38)",
            (eid, pid, json.dumps({"type": "set_volume", "before": 0.8, "after": 0.45})),
        ).lastrowid
        conn.execute("INSERT INTO evidence (pref_id, kind, ref_id, detail, created_at)"
                     " VALUES (?, 'correction', ?, ?, 'w')",
                     (pid, eid, json.dumps({"direction": "+15%", "delta_s": 8})))
        conn.execute("UPDATE action_log SET reaction='correct', quadrant='false_alarm',"
                     " resolved_at='w' WHERE id=?", (aid,))
        conn.commit()

        # 面板查询：一条 SQL 拿到"做了什么+你怎么反应+凭哪条信念"
        row = conn.execute(
            "SELECT a.reaction, a.quadrant, a.latency_ms, p.scope_key, p.confidence,"
            " (SELECT COUNT(*) FROM evidence e WHERE e.pref_id=p.id) AS n_evidence"
            " FROM action_log a JOIN preference p ON p.id=a.pref_id WHERE a.id=?",
            (aid,),
        ).fetchone()
        assert row[0] == "correct" and row[1] == "false_alarm" and row[2] == 38
        assert row[3] == "audio.form_factor:headphones" and row[4] == 0.55 and row[5] == 1
    finally:
        store.close()


def test_foreign_key_enforced(tmp_path):
    """evidence.pref_id 必须指向真实信念（foreign_keys=ON 生效）。"""
    _, conn = _db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO evidence (pref_id, kind, created_at)"
                     " VALUES (9999, 'event', 'w')")
