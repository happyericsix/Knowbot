"""音量归因决策表（A2）——规则编号与 docs/phase1-volume-attribution.md 严格对应。

判定是确定性的：同样的输入永远同样的 Verdict（含规则编号），
回放语料可以逐条断言。参数全部可配，默认值见文档 §6。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VolumeAttributionParams:
    # 用户校准 2026-10-25（docs §10）：
    # Q1 调整多为内容适配 → 内容可解释的纠正几乎不罚（gamma_content）
    # Q2 常在插入一段时间后才调 → 主窗 30s 三档衰减 + missed_need 学习窗放宽到 60s
    watch_window_s: float = 30.0
    strong_band_s: float = 2.0             # 强证据带：规则原始强度
    normal_band_s: float = 10.0            # 正常带：硬规则降为 soft
    c_window_s: float = 60.0               # 场景 C（missed_need）学习窗
    c_trailing_s: float = 10.0             # C 窗尾随安静期：用户调完静默即定案
                                           # （末值学习；2026-10-28 由"等满窗"改尾随去抖）
    settle_s: float = 1.5                   # 观察层去抖用；此处仅透传约定
    mag_floor: float = 0.03                 # 响度域幅度噪声底线
    meter_active_frac: float = 0.30         # active_frac ≥ 此值 = 内容在放
    gamma_hard: float = 0.5
    gamma_soft: float = 0.8
    gamma_content: float = 0.95             # 内容可解释（A2/A4）：几乎不罚，锚点照记
    gamma_extended: float = 0.9             # 扩展带（10–30s）：一律轻罚
    lambda_accept: float = 0.15
    burst_soft_threshold: int = 4           # 窗口内中间调整超过此数 → 强度降档


@dataclass(frozen=True)
class AgentAction:
    """action_log 里的一次决策（exec_mode='none' 表示"决定不出手"也落台账）。"""

    exec_mode: str                        # silent / notify / suggest / dry_run / none
    from_loudness: float | None = None    # 执行前（响度域）
    target_loudness: float | None = None  # 目标值（响度域）


@dataclass(frozen=True)
class UserAdjustment:
    """观察层去抖(settle)后上报的一次静止调整。"""

    delta_s: float                        # 距 agent 动作（无动作则距触发事件）秒数
    knob: str                             # master / session
    from_loudness: float | None = None    # 前一个静止值；首个调整可能没有
    to_loudness: float = 0.0
    burst_count: int = 1                  # 静止前的中间调整次数


@dataclass(frozen=True)
class MeterStats:
    """归因窗口内的电平采样汇总（受控采样，窗口结束即停）。"""

    mean: float
    peak: float
    active_frac: float


@dataclass(frozen=True)
class VolumeCase:
    agent: AgentAction | None = None
    trigger_matched: bool = False         # 触发事件（如 device_added）是否命中
    adjustment: UserAdjustment | None = None
    meter: MeterStats | None = None
    foreground: str | None = None         # 仅存证，不参与判定


@dataclass(frozen=True)
class Verdict:
    reaction: str | None      # accept / correct / ignore / missed_need
    quadrant: str | None      # correct_detection / false_alarm / missed_need / non_response
    rule: str                 # 规则编号（可解释引用，进 evidence）
    gamma: float | None = None        # 置信度乘性下调系数（学习器执行）
    lambda_up: float | None = None    # 置信度上调幅度（学习器执行）
    anchor_loudness: float | None = None  # 设备映射锚点（静止值）
    evidence_kind: str = "event"      # correction / event / action
    learn_device_pref: bool = False   # 是否触碰设备偏好信念
    reason: str = ""


def _is_real_execution(mode: str) -> bool:
    return mode in ("silent", "notify")


def attribute(case: VolumeCase, p: VolumeAttributionParams | None = None) -> Verdict:
    p = p or VolumeAttributionParams()
    adj = case.adjustment
    agent = case.agent
    real_exec = agent is not None and _is_real_execution(agent.exec_mode)

    # ---- 场景 B：窗口结束无任何静止调整 ----
    if adj is None:
        if real_exec:
            # B1：没碰 = 接受（默契靠累积：单次 λ 很小）
            return Verdict("accept", "correct_detection", "B1",
                           lambda_up=p.lambda_accept, evidence_kind="action",
                           learn_device_pref=True,
                           reason="窗口内未触碰该旋钮")
        return Verdict(None, None, "B0", reason="无执行无调整，无需归因")

    # ---- 旋钮身份：session 调整是内容适配，不动设备信念（N2）----
    if adj.knob != "master":
        return Verdict("ignore", None, "N2", evidence_kind="event",
                       reason="调整的是应用 session 音量，属内容适配",
                       anchor_loudness=None)

    # ---- 窗口外：自发调整（N3）。窗口按场景取：执行看 watch_window，未执行看 c_window ----
    window_s = p.watch_window_s if real_exec else p.c_window_s
    if adj.delta_s > window_s:
        return Verdict("ignore", None, "N3", evidence_kind="event",
                       reason="窗口外的自发调整，进观察草稿")

    # ---- 噪声底线（N1）。无前值（首个静止值）时无法算幅度，跳过该判定 ----
    mag = (abs(adj.to_loudness - adj.from_loudness)
           if adj.from_loudness is not None else None)
    if mag is not None and mag < p.mag_floor:
        return Verdict("ignore", None, "N1", reason="幅度低于噪声底线")

    # ---- 场景 C：agent 没真执行，用户自己动手 ----
    if not real_exec:
        # C1：missed_need——本该我出手；静止值直接是学习原料（draft 信念）
        # dry_run/suggest/none/无动作 同罪：没执行就没有可纠正对象
        return Verdict("missed_need", "missed_need", "C1",
                       anchor_loudness=adj.to_loudness, evidence_kind="event",
                       learn_device_pref=True,
                       reason="agent 未执行而用户手动设置")

    # ---- 场景 A：真执行后窗口内的 master 调整 = 纠正（强度分级）----
    active = case.meter is not None and case.meter.active_frac >= p.meter_active_frac
    user_down = adj.to_loudness < adj.from_loudness

    if agent.from_loudness is not None and agent.target_loudness is not None:
        agent_down = agent.target_loudness < agent.from_loudness
        opposite = user_down != agent_down
    else:
        opposite = True  # 无从比较方向时按反向（保守：重罚不如重锚点+轻罚，锚点总会写）

    if opposite:
        # 矩阵 A1–A4（内容可解释的 A2/A4 按用户校准 Q1 几乎不罚，锚点照记）
        if user_down and not active:
            rule, gamma = "A1", p.gamma_hard      # 安静中还嫌响：内容解释不了，铁证
        elif user_down and active:
            rule, gamma = "A2", p.gamma_content   # 多半是内容炸（校准 Q1）
        elif not user_down and active:
            rule, gamma = "A3", p.gamma_hard      # 内容洪亮仍不够：铁证
        else:
            rule, gamma = "A4", p.gamma_content   # 多半是补偿内容太轻（校准 Q1）
    else:
        # A5 同向继续：方向对了幅度欠——锚点必记，罚得轻（校准 Q3 确认）
        rule, gamma = "A5", p.gamma_soft

    # 强度衰减条款（三档时带，校准 Q2）：
    # ≤2s 原始强度；2–10s 硬规则降 soft；10–30s 扩展带一律轻罚
    if adj.delta_s > p.normal_band_s:
        gamma = max(gamma, p.gamma_extended)
    elif adj.delta_s > p.strong_band_s:
        gamma = max(gamma, p.gamma_soft)
    # 拖动寻值（校准 Q4 确认：中间全是噪声）→ 再降一档
    if adj.burst_count > p.burst_soft_threshold:
        gamma = max(gamma, p.gamma_soft)

    return Verdict("correct", "false_alarm", rule,
                   gamma=gamma, anchor_loudness=adj.to_loudness,
                   evidence_kind="correction", learn_device_pref=True,
                   reason=f"窗口内反向/同向调整，规则{rule}，γ={gamma}")
