"""记忆访问层：五张信念表的读写封装（A1 DDL 的操作面）。

与 EventStore 共享同一连接（同在事件循环线程，无跨线程竞争）。
判定在归因层/技能层，这里只做 SQL——保证每个写路径可单独测试。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime


def now_iso() -> str:
    """统一时间戳格式（面板/CLI/学习器共用；周窗统计按它解析）。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


_now = now_iso  # 模块内历史别名


class MemoryStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- 解析（A1 §5：exception > instance > class > global，一次查询） ----

    def resolve(self, pref_key: str, instance_key: str, class_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.* FROM preference p"
            " WHERE p.pref_key = ? AND p.state = 'active'"
            "   AND p.scope_key IN (?, ?, '')"
            "   AND NOT EXISTS (SELECT 1 FROM exception x"
            "       WHERE x.pref_key = p.pref_key AND x.scope_key = p.scope_key)"
            " ORDER BY CASE p.scope WHEN 'instance' THEN 0"
            "                        WHEN 'class' THEN 1 ELSE 2 END",
            (pref_key, instance_key, class_key),
        ).fetchall()

    # ---- 信念 ----

    def get_or_create(self, pref_key: str, scope: str, scope_key: str,
                      value: dict, confidence: float, source: str,
                      state: str = "active") -> sqlite3.Row:
        now = _now()
        self.conn.execute(
            "INSERT INTO preference (pref_key, scope, scope_key, value, confidence,"
            " source, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(pref_key, scope, scope_key) DO NOTHING",
            (pref_key, scope, scope_key, json.dumps(value, ensure_ascii=False),
             confidence, source, state, now, now),
        )
        self.conn.commit()
        return self.get(pref_key, scope, scope_key)

    def get(self, pref_key: str, scope: str, scope_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM preference WHERE pref_key=? AND scope=? AND scope_key=?",
            (pref_key, scope, scope_key),
        ).fetchone()

    def get_pref(self, pref_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM preference WHERE id=?", (pref_id,)).fetchone()

    def get_action(self, action_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM action_log WHERE id=?", (action_id,)).fetchone()

    def update_preference(self, pref_id: int, *, confidence: float | None = None,
                          source: str | None = None, state: str | None = None,
                          value: dict | None = None) -> None:
        sets, params = [], []
        if confidence is not None:
            sets.append("confidence=?"); params.append(float(confidence))
        if source is not None:
            sets.append("source=?"); params.append(source)
        if state is not None:
            sets.append("state=?"); params.append(state)
        if value is not None:
            sets.append("value=?")
            params.append(json.dumps(value, ensure_ascii=False))
        if not sets:
            return
        sets.append("updated_at=?"); params.extend([now_iso(), pref_id])
        self.conn.execute(f"UPDATE preference SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def ratchet_confirm(self, pref_id: int, lam: float) -> None:
        self.conn.execute(
            "UPDATE preference SET confidence = MIN(1.0, confidence + ? * (1 - confidence)),"
            " hits = hits + 1, last_hit_at = ?, updated_at = ? WHERE id = ?",
            (lam, _now(), _now(), pref_id))
        self.conn.commit()

    def ratchet_correct(self, pref_id: int, gamma: float) -> None:
        self.conn.execute(
            "UPDATE preference SET confidence = confidence * ?, updated_at = ? WHERE id = ?",
            (gamma, _now(), pref_id))
        self.conn.commit()

    def add_hit(self, pref_id: int) -> None:
        self.conn.execute(
            "UPDATE preference SET hits = hits + 1, last_hit_at = ? WHERE id = ?",
            (_now(), pref_id))
        self.conn.commit()

    def corrections_today(self, pref_id: int) -> int:
        today = datetime.now().astimezone().date().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE pref_id=? AND kind='correction'"
            " AND created_at >= ?", (pref_id, today),
        ).fetchone()[0]

    def set_state(self, pref_id: int, state: str) -> None:
        self.conn.execute("UPDATE preference SET state=?, updated_at=? WHERE id=?",
                          (state, _now(), pref_id))
        self.conn.commit()

    # ---- 运行时开关（meta 表，面板可写） ----

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                          " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))
        self.conn.commit()

    def append_anchor(self, device_scope_key: str, scalar: float) -> None:
        """锚点追加进设备映射信念（audio.device_map，实例级）。"""
        row = self.get("audio.device_map", "instance", device_scope_key)
        if row is None:
            value = {"anchors": [[_now(), scalar]]}
            self.get_or_create("audio.device_map", "instance", device_scope_key,
                               value, 0.5, "observed", state="active")
        else:
            value = json.loads(row["value"])
            value.setdefault("anchors", []).append([_now(), scalar])
            self.conn.execute(
                "UPDATE preference SET value=?, updated_at=? WHERE id=?",
                (json.dumps(value, ensure_ascii=False), _now(), row["id"]))
            self.conn.commit()

    # ---- 证据与台账 ----

    def find_event_id(self, sensor: str, type_: str, device_id: str | None,
                      around_mono: float, tol_s: float = 5.0) -> int | None:
        """按触发时刻反查 event_raw.id（补台账 trigger_event_id 的溯源链）。

        触发事件此刻应已落库（观察窗 ≥ flush 周期）；device_id 为 None 时不
        按 payload 过滤（如 session_unlock 无设备标识），取 ts 最接近的一行。
        """
        rows = self.conn.execute(
            "SELECT id, ts_mono, payload FROM event_raw"
            " WHERE sensor=? AND type=? AND ts_mono BETWEEN ? AND ?"
            " ORDER BY id DESC LIMIT 200",
            (sensor, type_, around_mono - tol_s, around_mono + tol_s),
        ).fetchall()
        best, best_diff = None, None
        for r in rows:
            if device_id is not None:
                try:
                    if json.loads(r["payload"]).get("device_id") != device_id:
                        continue
                except Exception:
                    continue
            diff = abs(r["ts_mono"] - around_mono)
            if best_diff is None or diff < best_diff:
                best, best_diff = r["id"], diff
        return best

    def insert_evidence(self, pref_id: int, kind: str, ref_id: int | None,
                        detail: dict, source_url: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO evidence (pref_id, kind, ref_id, detail, source_url, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (pref_id, kind, ref_id, json.dumps(detail, ensure_ascii=False),
             source_url, _now()))
        self.conn.commit()

    def log_action(self, skill: str, trigger_event_id: int | None, pref_id: int | None,
                   action: dict, exec_mode: str, ts_mono: float,
                   risk: str = "zero", undo_payload: dict | None = None,
                   latency_ms: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO action_log (ts_wall, ts_mono, skill, trigger_event_id, pref_id,"
            " action, undo_payload, risk, exec_mode, latency_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), ts_mono, skill, trigger_event_id, pref_id,
             json.dumps(action, ensure_ascii=False),
             json.dumps(undo_payload, ensure_ascii=False) if undo_payload else None,
             risk, exec_mode, latency_ms))
        self.conn.commit()
        return cur.lastrowid

    def finish_action(self, action_id: int, reaction: str | None, quadrant: str | None,
                      detail: dict | None = None,
                      trigger_event_id: int | None = None) -> None:
        self.conn.execute(
            "UPDATE action_log SET reaction=?, quadrant=?, reaction_detail=?,"
            " trigger_event_id=COALESCE(?, trigger_event_id), resolved_at=? WHERE id=?",
            (reaction, quadrant,
             json.dumps(detail, ensure_ascii=False) if detail else None,
             trigger_event_id, now_iso(), action_id))
        self.conn.commit()

    def set_action_trigger(self, action_id: int, trigger_event_id: int) -> None:
        """延迟回填触发事件溯源（prearm 台账：落账时事件尚在落库缓冲区）。"""
        self.conn.execute(
            "UPDATE action_log SET trigger_event_id=?"
            " WHERE id=? AND trigger_event_id IS NULL",
            (trigger_event_id, action_id))
        self.conn.commit()


# ---- 类级先验种子（社区库的占位实现；幂等） ----

def seed_defaults(memory: MemoryStore) -> None:
    """空库时种入类级安全默认（方案 §5：T+0 类级安全默认的来源）。

    scalar 域起步：响度域映射待校准锚点积累后引入（A1 §2.1 的演进路径）。
    """
    seeds = [
        ("global", "", {"scalar": 0.30}, 0.50, "community"),
        ("class", "audio.form_factor:headphones", {"scalar": 0.35}, 0.50, "community"),
        ("class", "audio.form_factor:headset", {"scalar": 0.40}, 0.50, "community"),
        ("class", "audio.form_factor:speakers", {"scalar": 0.25}, 0.50, "community"),
    ]
    for scope, key, value, conf, source in seeds:
        if memory.get("audio.volume", scope, key) is None:
            memory.get_or_create("audio.volume", scope, key, value, conf, source)
