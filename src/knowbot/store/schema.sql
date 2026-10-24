-- knowbot Phase 0 schema · user_version=1
-- 迁移规则：结构变更必须递增 user_version 并写迁移逻辑，禁止改历史。

CREATE TABLE IF NOT EXISTS event_raw (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    seq     INTEGER NOT NULL,   -- 进程内单调号，重启归零
    ts_mono REAL    NOT NULL,   -- time.monotonic() 秒；排序/延迟基准
    ts_wall TEXT    NOT NULL,   -- 本地时区 ISO8601；人读与对账
    sensor  TEXT    NOT NULL,   -- 命名空间，如 audio.device
    type    TEXT    NOT NULL,
    payload TEXT    NOT NULL    -- JSON；只存标识，永不存内容
);

CREATE INDEX IF NOT EXISTS idx_event_sensor ON event_raw(sensor, id);
CREATE INDEX IF NOT EXISTS idx_event_wall   ON event_raw(ts_wall);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
