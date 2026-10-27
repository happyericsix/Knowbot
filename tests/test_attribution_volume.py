"""A2 归因决策表黄金用例——docs/phase1-volume-attribution.md §9 的逐条固化。

归因是纯函数：同输入必同输出。以后线上任何归因争议，
先写成这里的用例，再改规则——规则改动必须过完全部黄金用例。
"""
from knowbot.attribution.volume import (
    AgentAction, MeterStats, UserAdjustment, Verdict, VolumeCase,
    attribute,
)

QUIET = MeterStats(mean=0.02, peak=0.05, active_frac=0.05)    # 安静
LOUD = MeterStats(mean=0.35, peak=0.70, active_frac=0.85)     # 内容在放

# agent 把 master 从 0.30 升到 0.60（反向纠正场景用：用户随后下调即"反向"）
ACT_UP = AgentAction(exec_mode="notify", from_loudness=0.30, target_loudness=0.60)
# agent 把 master 从 0.80 降到 0.45（同向/反向上调场景用）
ACT_DOWN = AgentAction(exec_mode="notify", from_loudness=0.8, target_loudness=0.45)


def test_case1_strong_correction_quiet_down():
    """#1 agent 上调后，用户 2s 内反向下调且安静：A1 强纠正。"""
    v = attribute(VolumeCase(
        agent=ACT_UP,
        adjustment=UserAdjustment(delta_s=1.5, knob="master",
                                  from_loudness=0.60, to_loudness=0.45),
        meter=QUIET))
    assert (v.reaction, v.quadrant, v.rule) == ("correct", "false_alarm", "A1")
    assert v.gamma == 0.5 and v.anchor_loudness == 0.45
    assert v.learn_device_pref and v.evidence_kind == "correction"


def test_case2_weak_correction_down_with_content():
    """#2 同上但内容在放：A2——校准 Q1（多为内容适配）：几乎不罚。"""
    v = attribute(VolumeCase(
        agent=ACT_UP,
        adjustment=UserAdjustment(delta_s=1.5, knob="master",
                                  from_loudness=0.60, to_loudness=0.45),
        meter=LOUD))
    assert v.rule == "A2" and v.gamma == 0.95
    assert v.anchor_loudness == 0.45  # 锚点照记


def test_case3_strong_correction_up_with_content():
    """#3 内容洪亮仍上调（agent 降了我升回去）：A3 强纠正。"""
    v = attribute(VolumeCase(
        agent=ACT_DOWN,
        adjustment=UserAdjustment(delta_s=1.2, knob="master",
                                  from_loudness=0.45, to_loudness=0.60),
        meter=LOUD))
    assert v.rule == "A3" and v.gamma == 0.5 and v.anchor_loudness == 0.60


def test_case4_weak_correction_up_quiet():
    """#4 安静中上调：A4——校准 Q1：可能只是补偿内容太轻，几乎不罚。"""
    v = attribute(VolumeCase(
        agent=ACT_DOWN,
        adjustment=UserAdjustment(delta_s=1.2, knob="master",
                                  from_loudness=0.45, to_loudness=0.60),
        meter=QUIET))
    assert v.rule == "A4" and v.gamma == 0.95


def test_case5_same_direction_refine():
    """#5 同向继续调（我降了你还继续降）：A5——锚点必记、罚得轻。"""
    v = attribute(VolumeCase(
        agent=ACT_DOWN,
        adjustment=UserAdjustment(delta_s=2.0, knob="master",
                                  from_loudness=0.45, to_loudness=0.32),
        meter=QUIET))
    assert v.rule == "A5" and v.gamma == 0.8 and v.anchor_loudness == 0.32


def test_case6_noise_floor():
    """#6 微调 0.01：N1 噪声忽略，不学。"""
    v = attribute(VolumeCase(
        agent=ACT_DOWN,
        adjustment=UserAdjustment(delta_s=3.0, knob="master",
                                  from_loudness=0.45, to_loudness=0.46),
        meter=QUIET))
    assert v.reaction == "ignore" and v.rule == "N1"
    assert not v.learn_device_pref and v.anchor_loudness is None


def test_case7_session_knob_not_device_preference():
    """#7 调的是浏览器 session 音量：N2——内容适配，绝不触碰设备信念。"""
    v = attribute(VolumeCase(
        agent=ACT_DOWN,
        adjustment=UserAdjustment(delta_s=2.0, knob="session",
                                  from_loudness=0.45, to_loudness=0.10),
        meter=LOUD))
    assert v.reaction == "ignore" and v.rule == "N2"
    assert not v.learn_device_pref


def test_case8_outside_window():
    """#8 45s 后才调（校准 Q2 后主窗 30s）：N3 窗外自发调整。"""
    v = attribute(VolumeCase(
        agent=ACT_DOWN,
        adjustment=UserAdjustment(delta_s=45.0, knob="master",
                                  from_loudness=0.45, to_loudness=0.70),
        meter=QUIET))
    assert v.reaction == "ignore" and v.rule == "N3"
    assert not v.learn_device_pref


def test_case9_no_touch_is_accept():
    """#9 窗口结束没碰：B1 接受，小步升置信（默契靠累积）。"""
    v = attribute(VolumeCase(agent=ACT_DOWN, adjustment=None, meter=QUIET))
    assert (v.reaction, v.quadrant, v.rule) == ("accept", "correct_detection", "B1")
    assert v.lambda_up == 0.15 and v.gamma is None


def test_case10_missed_need_manual_without_agent():
    """#10 agent 无信念没出手，用户手动设：C1 missed_need + 草稿信念原料。"""
    v = attribute(VolumeCase(
        agent=None, trigger_matched=True,
        adjustment=UserAdjustment(delta_s=4.0, knob="master",
                                  from_loudness=0.80, to_loudness=0.40),
        meter=LOUD))
    assert (v.reaction, v.quadrant, v.rule) == ("missed_need", "missed_need", "C1")
    assert v.anchor_loudness == 0.40 and v.learn_device_pref


def test_case11_dryrun_never_corrected():
    """#11 dry_run 后的调整按 C 处理——没执行就没有可纠正对象（防污染护栏 5）。"""
    v = attribute(VolumeCase(
        agent=AgentAction(exec_mode="dry_run", from_loudness=0.8, target_loudness=0.45),
        adjustment=UserAdjustment(delta_s=2.0, knob="master",
                                  from_loudness=0.45, to_loudness=0.60),
        meter=QUIET))
    assert v.rule == "C1" and v.reaction == "missed_need"
    assert v.reaction != "correct"


def test_case12_late_correction_softened():
    """#12 Δt=8s 的 A1 场景：出强证据带，降档为 γ=0.8。"""
    v = attribute(VolumeCase(
        agent=ACT_UP,
        adjustment=UserAdjustment(delta_s=8.0, knob="master",
                                  from_loudness=0.60, to_loudness=0.45),
        meter=QUIET))
    assert v.rule == "A1" and v.gamma == 0.8  # 规则不变，强度降档


def test_burst_softening():
    """§3 强度衰减条款：拖动寻值（burst>4）→ 降为 soft。"""
    v = attribute(VolumeCase(
        agent=ACT_UP,
        adjustment=UserAdjustment(delta_s=1.5, knob="master",
                                  from_loudness=0.60, to_loudness=0.45, burst_count=7),
        meter=QUIET))
    assert v.rule == "A1" and v.gamma == 0.8


def test_extended_band_light_penalty():
    """校准 Q2：10–30s 扩展带内的 A1 → 一律轻罚 γ=0.9。"""
    v = attribute(VolumeCase(
        agent=ACT_UP,
        adjustment=UserAdjustment(delta_s=20.0, knob="master",
                                  from_loudness=0.60, to_loudness=0.45),
        meter=QUIET))
    assert v.rule == "A1" and v.gamma == 0.9 and v.anchor_loudness == 0.45


def test_c_window_60s():
    """校准 Q2：未执行场景学习窗 60s——插入 40s 后才手动调，仍是 missed_need。"""
    v = attribute(VolumeCase(
        agent=None, trigger_matched=True,
        adjustment=UserAdjustment(delta_s=40.0, knob="master",
                                  from_loudness=0.80, to_loudness=0.40),
        meter=LOUD))
    assert (v.reaction, v.rule) == ("missed_need", "C1")
    assert v.anchor_loudness == 0.40


def test_c_window_expired():
    """学习窗外（70s）的手动调整：N3 自发调整，不学。"""
    v = attribute(VolumeCase(
        agent=None, trigger_matched=True,
        adjustment=UserAdjustment(delta_s=70.0, knob="master",
                                  from_loudness=0.80, to_loudness=0.40),
        meter=LOUD))
    assert v.rule == "N3" and v.reaction == "ignore"


def test_none_mode_is_auditable_non_action():
    """A1 修订：exec_mode='none'（决定不出手）也落台账；用户手动设 → missed_need。"""
    v = attribute(VolumeCase(
        agent=AgentAction(exec_mode="none"),
        trigger_matched=True,
        adjustment=UserAdjustment(delta_s=3.0, knob="master",
                                  from_loudness=0.8, to_loudness=0.4),
        meter=QUIET))
    assert v.rule == "C1" and v.quadrant == "missed_need"
