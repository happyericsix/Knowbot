# Phase 1 事件聚合层（A3 入场券）

> 状态：已实现并经真实语料回归 · 2026-10-25
> 实现：`src/knowbot/aggregation/audio.py`（纯函数折叠器 + 总线服务）
> 语料：`tests/fixtures/audio_monitor_wake.jsonl`（显示器唤醒真实事件）

## 0. 问题与真实语料的两个发现

原始问题（设计时预判）：设备状态爆发需要折叠，否则技能被抖动反复触发。

**发现一（预期内）**：显示器唤醒产生爆发——但不是"一个设备翻 11 次"。

**发现二（超出预期）**：2026-10-25 晨的真实语料显示，唤醒风暴是
**3 个 NVIDIA 端点（DP/DP/HDMI 各一路）协同翻转**，每路只翻 2 次、
分两段（间隔 89 秒）共 11 条 state_changed。

推论：**按"单设备翻转次数"设 churn 阈值永远抓不住这种风暴**——
单路只有 2 次（正常插拔也是 1-2 次）。正确信号是**协同指纹**：
一次人类插拔动作不会有邻居，系统级抖动必然多端点同步。

## 1. 架构定位（快慢分离的落实）

```
audio.device(原始事件) ──┬──→ 快反射层（pre-arm：直接订阅原始事件，毫秒级）
                         └──→ 聚合层 ──→ audio.flow(语义事件) ──→ 慢路径/学习/统计
```

- **聚合只服务慢路径**：settle 延迟（≥2s）只影响"定案"，不影响"出手"；
- 语义事件回发布到总线（`audio.flow` 命名空间，payload 带 `derived:true`），
  与原始事件同库同回放语料——不另建表，schema 不变（v2 已够）；
- 快路径需要预判时（如 pre-arm）订阅原始流，与本项目"预埋"设计一致。

## 2. 折叠规则

| 规则 | 行为 |
|---|---|
| 状态折叠 | `state_changed`/`device_added` 按 device_id 缓冲；安静 `settle_s` 定案为 `device_settled`（final_state/flips/span_ms/co_flipping/churn） |
| 拔出即定案 | `device_removed` 立即定案（final=removed），不等 settle |
| 默认输出折叠 | `default_changed` 按 (flow, role) 缓冲；同设备连续重复 → 一条 `default_output`（repeats 计数）；换设备旧结论立即定案 |
| 安全阀 | 窗口跨度 ≥ `max_span_s` 强制定案（防缓冲永悬） |
| 基线直通 | snapshot 不折叠（回放基线数据） |

**churn 判定（两级或）**：
1. 单设备翻转 ≥ `churn_flips`(3)：自身不稳定；
2. 协同邻居 ≥ `co_devices`(2)：锚点 ±`co_window_s`(3s) 内还有 ≥2 个别的设备在翻
   ——显示器唤醒/驱动重置的指纹（发现二的直接落地）。

## 3. Phase 1 技能的消费方式

- 音量技能触发条件：`device_settled(final_state=active)` 且
  `churn=false` 且 form_factor ∈ {headphones, headset}——
  显示器端点（hdmi）与抖动端点（churn）天然出局，无需特判；
- `default_output`：学习"输出指向"偏好的触发源；
- `co_flipping` 字段保留在 payload：归因和学习可引用（如"抖动期内的观察降权"）。

## 4. 实现要点（被测试逼出来的两个修正）

1. **协同计算不能按当前时间修剪历史**：两段爆发间隔 89 秒，晚到的定案
   仍需锚点附近的历史。最终方案：翻转时间线用**容量界定**的滑动窗口
   （deque maxlen=256），不按时间修剪；
2. **同批定案共享证据**：一条 emit 的修剪会毁掉同批后续 emit 的邻居
   证据（真实语料回归抓出：cb co=2 而 6e/2c co=0）——上述容量方案顺带解决。

## 5. 参数（config 可调）

| 参数 | 默认 | 含义 |
|---|---|---|
| agg_settle_s | 2.0 | 定案安静窗 |
| agg_churn_flips | 3 | 单设备 churn 阈值 |
| agg_co_window_s | 3.0 | 协同窗口 |
| agg_co_devices | 2 | 协同邻居数阈值（2 = 至少三路同翻） |
| agg_max_span_s | 60.0 | 安全阀 |
| agg_tick_interval | 0.5 | 服务层时钟推进周期 |

## 6. 验证

- 9 个合成黄金用例 + **真实语料回归**（11 条 → 6 条定案、3 端点、
  flips 无损、协同全命中、末态 active）；
- 真实语料已固化为 `tests/fixtures/audio_monitor_wake.jsonl`，
  任何聚合规则改动必须过这条回归。

## 7. 待办（Phase 1 期间）

- USB/蓝牙传感器接入后，同款折叠模式复用（独立 Folder，不提前抽象共享基类）；
- `default_changed` 在拔出瞬间可能连发多条不同角色——观察真实数据后再定折叠粒度。
