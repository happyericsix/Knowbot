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

-- ============================================================
-- v2 · Phase 1 记忆库（A1）：五张信念表，设计文档见 docs/phase1-memory-ddl.md
-- 原则：事实(event_raw)与信念分离；全部幂等 CREATE，只增不改。
-- ============================================================

CREATE TABLE IF NOT EXISTS preference (
    id          INTEGER PRIMARY KEY,
    pref_key    TEXT NOT NULL,               -- 旋钮名：audio.volume / startup.launch_set
    scope       TEXT NOT NULL,               -- global / class / instance
    scope_key   TEXT NOT NULL DEFAULT '',    -- '' / audio.form_factor:headphones / audio.device:{id}
    value       TEXT NOT NULL,               -- JSON：旋钮值形态各异（数/列表/字符串）
    confidence  REAL NOT NULL DEFAULT 0.5,   -- 0..1，执行分级依据
    source      TEXT NOT NULL,               -- explicit / observed / inferred / community / research
    state       TEXT NOT NULL DEFAULT 'draft',
    hits        INTEGER NOT NULL DEFAULT 0,  -- 命中次数（频次淘汰门槛用）
    last_hit_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (pref_key, scope, scope_key)      -- 一旋钮一作用域一条现行信念；global 的 scope_key 归一为 ''
);
CREATE INDEX IF NOT EXISTS idx_pref_resolve ON preference(pref_key, state, scope_key);

CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY,
    pref_id      INTEGER NOT NULL REFERENCES preference(id),
    kind         TEXT NOT NULL,              -- event / action / correction / calibration / external
    ref_id       INTEGER,                    -- kind=event → event_raw.id；kind=action → action_log.id
    detail       TEXT,                       -- JSON 摘要：归因时刻的观测快照（方向/幅度/电平/时间差）
    source_url   TEXT,                       -- kind=external 时：来源链接
    retrieved_at TEXT,                       -- kind=external 时：检索日期（先验会过期）
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_pref ON evidence(pref_id, id);

CREATE TABLE IF NOT EXISTS action_log (
    id               INTEGER PRIMARY KEY,
    ts_wall          TEXT NOT NULL,
    ts_mono          REAL NOT NULL,
    skill            TEXT NOT NULL,          -- volume / startup / ...
    trigger_event_id INTEGER,                -- event_raw.id
    pref_id          INTEGER,                -- 命中的 preference.id
    action           TEXT NOT NULL,          -- JSON：{type, target, before, after}
    undo_payload     TEXT,                   -- JSON：撤销载荷（回到"我的贡献"之前的用户最后值）
    risk             TEXT NOT NULL,          -- zero / low / medium
    exec_mode        TEXT NOT NULL,          -- silent / notify / suggest / dry_run
    latency_ms       REAL,                   -- 触发事件→执行实测延迟
    reaction         TEXT,                   -- null(未定) / accept / correct / reject / ignore / missed_need
    reaction_detail  TEXT,                   -- JSON：A2 归因证据摘要
    quadrant         TEXT,                   -- correct_detection / false_alarm / missed_need / non_response
    resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_skill ON action_log(skill, id);

CREATE TABLE IF NOT EXISTS proposal (
    id             INTEGER PRIMARY KEY,
    ts_wall        TEXT NOT NULL,
    kind           TEXT NOT NULL,            -- new_device / generalize_l1 / generalize_l2 / new_app / suppress
    content        TEXT NOT NULL,            -- JSON 提案体（含可解释引用）
    confidence     REAL NOT NULL,            -- 来源置信 × 迁移折扣，封顶不高于来源
    origin_pref_id INTEGER,                  -- 来源信念（追溯泛化路径）
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending / accepted / rejected / expired
    decided_at     TEXT,
    decided_via    TEXT,                     -- bubble / panel / timeout
    notify_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exception (
    id         INTEGER PRIMARY KEY,
    pref_key   TEXT NOT NULL,
    scope_key  TEXT NOT NULL,                -- 被抑制对象（''=整旋钮）
    reason     TEXT,                         -- proposal_rejected_x2 / user_explicit / noisy_source
    expires_at TEXT,                         -- NULL = 永久
    created_at TEXT NOT NULL,
    UNIQUE (pref_key, scope_key)
);
