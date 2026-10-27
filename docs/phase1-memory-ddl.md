# Phase 1 记忆库 DDL（A1 入场券）

> 状态：待评审 · 2026-10-25 · 对应方案 §4（记忆系统设计）
> 读完每节请在评审时表态：同意 / 有异议（写在哪条都行）

## 0. 总览：一张录像带 + 五张信念表

```
event_raw ──── 事实（不可变，已有，本设计零改动）
    │ 引用
    ▼
preference ── 信念：对某个旋钮的当前判断（带置信度与生命周期）
    ▲ 支撑
evidence ──── 证据：每条信念"凭什么"（事件/纠正/校准/外部来源）
    │
action_log ── 台账：agent 干了什么、你如何反应、四象限归档
    │ 触发
proposal ──── 提案：泛化产出，待确认区的数据底座
exception ─── 例外：一票否决的抑制记忆，优先级最高
```

## 1. 设计原则（五条，每张表都从这推出）

1. **事实与信念分离**：event_raw 只追加不修改；五张新表全部可演化。信念永远可以重建（从 event_raw + 算法），反过来不行。
2. **一切可溯源**：任何偏好都能回答"你为什么信这个"——沿 evidence 链走到原始事件或外部 URL。面板的每一行解释文案都从这里生成。
3. **生命周期显式化**：`draft → pending → active → stale → archived` 是列不是约定；待确认区（pending）是 UI 一等视图。
4. **置信度是数据不是代码**：数值存库，棘轮参数（λ/γ/阈值）全部走 config；更新历史不存库（在 evidence 里），库中只存当前值。
5. **作用域链静态可查**：`exception > instance > class > global` 的解析是一次确定性的 SQL，不靠运行时魔法。

## 2. 逐表 DDL

### 2.1 preference —— 信念

```sql
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
    UNIQUE (pref_key, scope, scope_key)      -- 一旋钮一作用域只有一条现行信念
);
CREATE INDEX IF NOT EXISTS idx_pref_resolve ON preference(pref_key, state, scope_key);
```

字段取舍说明：

- **value 用 JSON 而不是列**：音量是数、启动组合是列表、输入法是字符串——信封统一、载荷自由，和 event_raw 同一哲学。查询用 `json_extract`。
- **scope_key 用 `''` 表示 global**（不用 NULL）：SQLite 的 UNIQUE 对 NULL 各算各的，会让 global 出现重复行；访问层统一归一化为空串。
- **UNIQUE 三元组**是"信念的唯一性"约束：同一旋钮同一作用域，新证据到来时是**更新**这一行（置信度棘轮），不是插新行。
- 响度域原则（§4.6）落在这里：`audio.volume` 的 value 存 `{"loudness": 0.45}`（感知域），不存 scalar；每设备的 scalar↔响度映射是另一条 pref_key=`audio.device_map` 的实例级信念，value 存锚点对数组。

### 2.2 evidence —— 证据

```sql
CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY,
    pref_id      INTEGER NOT NULL REFERENCES preference(id),
    kind         TEXT NOT NULL,   -- event / action / correction / calibration / external
    ref_id       INTEGER,         -- kind=event → event_raw.id；kind=action → action_log.id
    detail       TEXT,            -- JSON 摘要：当时的观测（方向/幅度/电平/时间差）
    source_url   TEXT,            -- kind=external 时：规格页/口碑链接
    retrieved_at TEXT,            -- kind=external 时：检索日期（先验会过期）
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_pref ON evidence(pref_id, id);
```

- kind 五值：`event`（观察到的使用事实）、`action`（agent 执行后的结果）、`correction`（用户纠正，最重要的一种）、`calibration`（三键气泡等显式校准）、`external`（研究层外部先验）。
- **detail 存"归因时刻的快照"**：纠正发生时的电平、调整方向、与 agent 动作的时间差——这些从 event_raw 反推代价高，纠正发生时顺手存下来，面板解释直接用。

### 2.3 action_log —— 台账（四象限与风控的地基）

```sql
CREATE TABLE IF NOT EXISTS action_log (
    id               INTEGER PRIMARY KEY,
    ts_wall          TEXT NOT NULL,
    ts_mono          REAL NOT NULL,
    skill            TEXT NOT NULL,   -- volume / startup / ...
    trigger_event_id INTEGER,         -- event_raw.id
    pref_id          INTEGER,         -- 命中的 preference.id
    action           TEXT NOT NULL,   -- JSON：{type, target, before, after}
    undo_payload     TEXT,            -- JSON：撤销所需（恢复"我的贡献"前的用户最后值）
    risk             TEXT NOT NULL,   -- zero / low / medium
    exec_mode        TEXT NOT NULL,   -- silent / notify / suggest / dry_run
    latency_ms       REAL,            -- 触发事件→执行 实测延迟（50ms 预算的验证数据）
    reaction         TEXT,            -- null(未定) / accept / correct / reject / ignore / missed_need
    reaction_detail  TEXT,            -- JSON：A2 归因证据摘要
    quadrant         TEXT,            -- correct_detection / false_alarm / missed_need / non_response
    resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_skill ON action_log(skill, id);
```

- `reaction` / `quadrant` 的合法值就是 **A2 归因决策表的输出接口**——A1 把列留好，A2 只负责填判定规则。
- dry_run 行也进本表（exec_mode 标注），"只记录我会做什么"由此可直接查询。

### 2.4 proposal —— 提案

```sql
CREATE TABLE IF NOT EXISTS proposal (
    id             INTEGER PRIMARY KEY,
    ts_wall        TEXT NOT NULL,
    kind           TEXT NOT NULL,    -- new_device / generalize_l1 / generalize_l2 / new_app / suppress
    content        TEXT NOT NULL,    -- JSON 提案体（含可解释引用）
    confidence     REAL NOT NULL,    -- 来源置信 × 迁移折扣(0.7)，封顶不高于来源
    origin_pref_id INTEGER,          -- 来源信念（追溯泛化路径）
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending / accepted / rejected / expired
    decided_at     TEXT,
    decided_via    TEXT,             -- bubble / panel / timeout
    notify_count   INTEGER NOT NULL DEFAULT 0        -- 同实体最多提醒一次的计数依据
);
```

### 2.5 exception —— 例外（一票否决）

```sql
CREATE TABLE IF NOT EXISTS exception (
    id         INTEGER PRIMARY KEY,
    pref_key   TEXT NOT NULL,
    scope_key  TEXT NOT NULL,        -- 被抑制的对象（空串=整旋钮）
    reason     TEXT,                 -- proposal_rejected_x2 / user_explicit / noisy_source
    expires_at TEXT,                 -- NULL = 永久
    created_at TEXT NOT NULL,
    UNIQUE (pref_key, scope_key)
);
```

- 被拒提案落此处（方案 §9：同提案同实体最多一次，拒两次永久静默）；解析顺序永远第一位。

## 3. 生命周期状态机

```
draft ──(观察次数够/频次门槛)──▶ pending ──(用户确认 或 置信≥0.85)──▶ active
  ▲                                                              │
  │              连续两次纠正/周纠正率>30% → 降级只建议+标红        │ 半衰期衰减+频次不足
  └────────────────── stale ◀────────────────────────────────────┘
                          └──▶ archived（只读，可清理）
```

| 迁移 | 触发 | 依据 |
|---|---|---|
| draft→pending | 观察样本 ≥ N（config） | 频次门槛防一次性噪音 |
| pending→active | 用户在面板/气泡确认，或显式输入 | 显式与推断隔离红线 |
| active 内升降 | 置信度棘轮（§4） | 0.85/0.70/0.50 执行分级（config） |
| active→stale | 半衰期衰减至阈值下 / 命中归零超时 | 频率归一衰减（评审重点，见下） |
| 任意→archived | 用户手动 / 清理任务 | 保留可查 |

**衰减规则（对方案 §4.3 的修正）**：衰减按**状态门控 + 事件频率归一**——explicit 来源与 active 高置信不随墙钟衰减；observed/inferred 按该偏好自身触发频率归一半衰期（一周用一次的 HDMI 音频不能按两周半衰期淘汰）。此修正源于评审第 2 条，写死进 Phase 1 学习器。

## 4. 置信度动力学（落库方式）

数值只存当前值，更新是两条参数化 UPDATE（λ/γ 来自 config）：

```sql
-- 确认（不对称棘轮上调，越接近 1 越难涨）
UPDATE preference SET confidence = MIN(1.0, confidence + :lambda * (1 - confidence)),
       hits = hits + 1, updated_at = :now WHERE id = :id;
-- 纠正（乘性下调）
UPDATE preference SET confidence = confidence * :gamma, updated_at = :now WHERE id = :id;
```

变化历史不建表：`evidence(kind=action/correction)` 按时间排列即为完整轨迹，可离线重放验证动力学参数（回放基建直接复用）。

## 5. 作用域解析（决策时的一次查询）

```sql
SELECT p.*, e.cnt FROM preference p
WHERE p.pref_key = :key AND p.state = 'active'
  AND p.scope_key IN (:instance_key, :class_key, '')
  AND NOT EXISTS (SELECT 1 FROM exception x
                  WHERE x.pref_key = p.pref_key AND x.scope_key = p.scope_key)
ORDER BY CASE p.scope WHEN 'instance' THEN 0 WHEN 'class' THEN 1 ELSE 2 END;
```

- 一次拿全链，取首行为执行依据，其余作降级候选；exception 经 `NOT EXISTS` 一票否决。
- 平局不可能：三元组 UNIQUE 保证同层只有一条。

## 6. schema v2 迁移

- `PRAGMA user_version: 1 → 2`；全部 `CREATE TABLE IF NOT EXISTS`，**幂等、只增不改**。
- 升级时机：任何新代码进程以写模式打开库即自动完成（daemon 下次重启时生效）；对运行中的 v1 daemon 零影响，event_raw 数据不动。
- 此后原则不变：结构变更必须递增 user_version 并在本文档追加迁移说明。

## 7. 旗舰场景走查（五表协同的一昼夜）

```
T+0    耳机插入 → event_raw: audio.device/device_added (E1)
T+0    音量技能查链：exception(无) → instance(无) → class=headphones(0.55,
       source=community, {"loudness":0.45}) → global(0.30)
T+40ms pre-arm 早已布防；本次按类级通知执行 → action_log(A1):
       {skill:volume, trigger:E1, pref:P_class, exec_mode:notify, latency_ms:38}
T+8s   你手动调高 → 归因(A2)：同旋钮、8s 内、反向、电平低 → 判定 correct
       → action_log 回填 {reaction:correct, quadrant:false_alarm, detail:{...}}
       → evidence {pref:P_class, kind:correction, ref:E2, detail:{direction:+15%}}
       → 置信棘轮 0.55 → 0.275 (γ=0.5)
次日   再插入 → 再执行 → 你没碰 → reaction=accept → 0.275 + λ(1-0.275)
第3天  三键气泡"太轻/刚好/太吵" → 点"刚好" → evidence(kind=calibration)
       → 实例级 preference 诞生：draft →(直接)→ active，source=explicit, 置信 0.9
此后   该耳机每次插入命中 instance 层，零模型零研究，查表即得
```

## 8. 留给 A2 的接口（A1 不越界）

- `action_log.reaction ∈ {accept, correct, reject, ignore, missed_need, null}`
- `action_log.quadrant ∈ {correct_detection, false_alarm, missed_need, non_response}`
- `evidence.detail` 的 JSON 结构由 A2 的归因决策表定义（每技能一张表，音量先行）

## 9. 明确不做

不做向量/embedding、不做多用户、不给 event_raw 加列、不做偏好历史表（轨迹=evidence 时间线）、阈值不进 DDL（全在 config）。
