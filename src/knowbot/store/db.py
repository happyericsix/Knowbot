"""event_raw 落库：单写者 + 批量提交。

连接上的所有操作都发生在事件循环线程（append 由总线分发器同步
调用，flush 由周期任务调用），无跨线程竞争。CLI（tail/stats/dump）
以独立进程只读连接打开——WAL 允许一写多读。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from ..envelope import Event

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

SCHEMA_VERSION = 2  # v2 = Phase 1 记忆库五表（docs/phase1-memory-ddl.md）


class EventStore:
    def __init__(self, path: str | Path, batch_size: int = 200, flush_interval: float = 0.5) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # 技能/记忆层按列名取值
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # 全部 DDL 幂等（IF NOT EXISTS）：旧库打开即自动升级到当前版本
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._buffer: list[tuple] = []
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.flushed = 0

    # ---- 写路径（事件循环线程） ----

    def append(self, event: Event) -> None:
        self._buffer.append(event.to_row())
        if len(self._buffer) >= self.batch_size:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        self._conn.executemany(
            "INSERT INTO event_raw (seq, ts_mono, ts_wall, sensor, type, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            self._buffer,
        )
        self._conn.commit()
        self.flushed += len(self._buffer)
        self._buffer.clear()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            self._flush()

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._flush()
        finally:
            self._conn.close()


# ---- 只读查询（CLI 与回放导出共用；独立连接，WAL 只读安全） ----


def _connect_readonly(path: str | Path) -> sqlite3.Connection:
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_event(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "seq": row["seq"],
        "ts_mono": row["ts_mono"],
        "ts_wall": row["ts_wall"],
        "sensor": row["sensor"],
        "type": row["type"],
        "payload": json.loads(row["payload"]),
    }


def tail_events(path: str | Path, n: int = 20) -> list[dict]:
    """最近 n 条，新事件在前。"""
    with _connect_readonly(path) as conn:
        rows = conn.execute(
            "SELECT * FROM event_raw ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def iter_events(path: str | Path, wall_from: str | None = None, wall_to: str | None = None):
    """按 id 升序导出；wall_from/wall_to 为 ts_wall 的 ISO 前缀（含/含）。"""
    sql = "SELECT * FROM event_raw"
    conds, params = [], []
    if wall_from:
        conds.append("ts_wall >= ?")
        params.append(wall_from)
    if wall_to:
        conds.append("ts_wall <= ?")
        params.append(wall_to)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id"
    conn = _connect_readonly(path)
    try:
        for row in conn.execute(sql, params):
            yield _row_to_event(row)
    finally:
        conn.close()


def sensor_stats(path: str | Path) -> dict:
    conn = _connect_readonly(path)
    try:
        sensors = {}
        for row in conn.execute(
            "SELECT sensor, COUNT(*) AS c, MIN(ts_wall) AS first, MAX(ts_wall) AS last"
            " FROM event_raw GROUP BY sensor ORDER BY c DESC"
        ):
            sensors[row["sensor"]] = {"count": row["c"], "first": row["first"], "last": row["last"]}
        total = conn.execute("SELECT COUNT(*) FROM event_raw").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "user_version": version, "sensors": sensors, "generated_at": time.time()}
