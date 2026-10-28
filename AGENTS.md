# AGENTS.md — knowbot 项目上下文交接

> 本文件给任何进入本仓库的 AI agent（或与本项目建设者对话的其他 agent）
> 提供完整的项目心智模型：它是什么、为什么这样设计、现在在哪、
> 词汇怎么讲。人读请从 README.md 开始。

## 一句话定义

**本机常驻、无对话界面、事件驱动的个人 agent**：不听你说话，而是听
你的操作系统——事件 → 查记忆 → 抢在感知/伤害前适配 → 观察修正 →
学习回写记忆。数据全部本地（一个 SQLite 文件）。

核心循环：**事件 → 查记忆 → 适配 → 观察 → 学习回写**。

## 设计哲学（十条，回答"为什么"时的依据）

1. **记忆不是 RAG 问题，是查表问题**：偏好 = 带来源与置信度的类型化
   策略（trigger+scope+action+confidence），不需要向量/embedding/模糊召回。
   护城河在行动层，不在记忆层。
2. **快慢分离**：毫秒级反射纯规则永远无模型；模型只在研究层
   "结构化进、结构化出"的边界内，永不出现在决策路径。
3. **信任是挣来的，不是给的**：置信度分级执行（≥0.85 静默 / ≥0.70
   通知 / ≥0.50 建议），连续被纠正自动降档，explicit 用户意志永远
   压过 inferred 统计。
4. **方向不对称**：安全方向的调整可自动执行（音量只降不升的 pre-arm），
   风险方向永远只建议。
5. **宁可不学，不可学错**：归因歧义一律判 ignore；记忆污染是死亡螺旋
   的起点（错误记忆→错误动作→失去信任）。
6. **事实与信念分离**：event_raw 是不可变的录像带；五张信念表
   （preference/evidence/action_log/proposal/exception）可演化，
   一切可溯源（"你为什么信这个"沿证据链走到原始事件）。
7. **安全是结构产物不是纪律产物**：所有动作过同一条窄门（置信度闸门
   → dry-run 开关 → 台账不可豁免 → undo 载荷），新技能出生即继承全部护栏。
8. **听事件，不听内容**：只采集标识（进程名/设备ID/VID:PID）；文件路径、
   日历忙闲属"标识中间地带"需显式授权；窗口文本/屏幕/键盘永不动。
9. **计算礼貌**：默认纯 CPU、用完即卸、只在空闲窗口推理、前台重负载
   让路、通知有每日预算——agent 的存在感与打扰度成反比才是好 agent。
10. **主动性的仪表盘**：四象限记账（出手/沉默 × 需要/不需要，借自清华
    ProactiveAgent），周 False-Alarm 率 <10% 是全系统健康线。

## 词汇表（对话时请使用这些词的准确含义）

| 词 | 含义 |
|---|---|
| Skill（技能） | 能力的封装单位=信任的计量单位：触发/查询/执行/观察/撤销五元组+元数据 |
| pre-arm（预埋） | 趁端点休眠预设安全音量，把拔耳机的竞态变成预先布防 |
| 作用域链 | exception > instance > class > global，一次 SQL 解析 |
| 锚点 | 用户纠正时的静止值，设备映射（scalar↔响度）收敛的原料 |
| 响度域 | 偏好存感知响度不存系统刻度："偏好存感知域，设备只是编码" |
| churn（抖动） | 端点不稳定标记：单设备翻转≥3 或多端点协同翻转（显示器唤醒指纹） |
| 影子模式（dry-run） | 只记"我会做什么"不执行；每个新能力先当影子再拿方向盘 |
| 台账（action_log） | 不可豁免的动作审计：前后状态/undo载荷/延迟/四象限 |
| missed_need | 四象限之一：agent 没出手而用户手动做了——最好的学习原料 |
| 三键校准 | "太轻/刚好/太吵"气泡，一次点击=数周被动观测 |
| 计算礼貌 | 见哲学 9 |

## 架构地图（代码在哪）

```
src/knowbot/
├── envelope.py      事件信封（seq+双时间戳+命名空间+payload）
├── bus.py           有界队列总线（满即丢弃计数，绝不反压）
├── daemon.py        装配+优雅退出+协议注册
├── sensors/         心跳 / audio_device(COM STA) / winmsg(消息窗口三合一) / meter / idle
├── aggregation/     audio.py：爆发折叠+协同churn指纹（audio.flow 命名空间）
├── attribution/     volume.py：归因决策表纯函数（A1-A5/N1-N3/B/C 规则编号）
├── store/           db.py（event_raw+批量落库）/ memory.py（五表操作面+溯源反查）
├── executors/       volume.py（含 AGENT_CONTEXT_GUID：agent写的身份标识）
├── skills/          volume.py：首个 Skill 完整闭环（dry-run 影子运行中）
├── learning/        learner.py：Verdict → 棘轮/证据/锚点/草稿；apply_calibration 三键校准
├── notify/          toast.py（PowerShell WinRT，支持按钮）/ protocol.py（knowbot: URI 回传）
└── panel/           server.py + static/：概览(周FA率)/偏好/台账/事件流 + 校准/确认/撤销/暂停
docs/                A1 DDL / A2 归因 / A3 聚合 / 设计修订登记 / 场景目录37例 / 进度报告
tests/               85 个测试（黄金用例+真实语料回归 fixtures/）
```

## 当前状态（2026-10-28 深夜）

- Phase 0 ✅；入场券 A1/A2/A3 ✅；Phase 1 ①②③ ✅
- **音量技能已真执行（dry-run 关闭，§18）**：当前唯一真调音量的是 ZG2
  （explicit/0.9 → 静默档 25%，用户两次确认）；其余信念全在建议档不动音量
- pre-arm 真执行中（10-27 起，台账化+暂停覆盖+冷却，§15/§16）
- 交互层六轮真机打磨闭环（§13 触发范围/§14 时效+补决策/§17 学习窗尾随）
- 三键校准闭环、周窗 FA 仪表、台账溯源全部在线
- **trickle 启动**：空闲传感器上线（system.idle：idle_begin/end，Phase 3
  节律记忆的原料）；代码已同步 GitHub（ssh over 443）
- **Phase 3 入场券已成稿**（docs/phase3-startup-skill.md）：启动技能 v1 =
  解锁后开工组合助手，建议档起步；零新表零新传感器（节律住在记忆里）；
  S1-S7 归因草案；待用户过目后实施（RhythmObserver 可先行）
- 下一步：真执行期的 A 归因积累 → FA 仪表读数 → 启动技能实施 → WiFi 传感器
- 详见 docs/phase-progress.md（进度）与 docs/design-amendments.md（与方案v0.1的偏差）

## 与本项目的 agent 对话时

- 谈设计变更：先读 docs/design-amendments.md，修订须登记并给理由；
- 谈新场景：先归入五条路径之一（反射/查表/节律/提案/观察），
  过五问：信号是结构化事件吗？动作是可逆旋钮吗？有客观纠正信号吗？
  偏好重复吗？有时间窗口吗？
- 谈代码：改归因/聚合规则必须过全部黄金测试；线上 bug 先收语料再修；
- 谈测试纪律：自动化测试只用 Fake 执行器，真机写操作是显式手动验证；
- 谈模型：v1 零模型是设计不是缺失；SFT 的数据源是台账（接受/纠正史）。

## 快速上手（Windows）

```bash
.venv/Scripts/python -m knowbot daemon   # 前台跑
.venv/Scripts/python -m knowbot tail     # 看最近事件
.venv/Scripts/python -m pytest -q        # 85 个测试
scripts\daemon_stop.cmd / daemon_start.vbs  # 常驻服务停/启
```
