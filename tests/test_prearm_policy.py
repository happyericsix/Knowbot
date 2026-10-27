"""pre-arm 决策纯函数测试（真实执行链路属真机手动验证，不进自动化）。"""
from knowbot.executors.volume import decide_prearm


def test_lowers_high_volume_to_safe():
    """休眠端点高于安全值 → 压到安全值。"""
    assert decide_prearm("A", "B", 0.85, 0.2) == 0.2


def test_skips_when_already_safe():
    """已低于安全值 → 不动（方向不对称：不抬升）。"""
    assert decide_prearm("A", "B", 0.15, 0.2) is None
    assert decide_prearm("A", "B", 0.2, 0.2) is None  # 恰好等于也不动


def test_only_lower_false_sets_always():
    """关闭只降开关 → 低于也设到安全值（配置逃生口）。"""
    assert decide_prearm("A", "B", 0.05, 0.2, only_lower=False) == 0.2


def test_same_or_missing_device_no_op():
    assert decide_prearm(None, "B", 0.9, 0.2) is None
    assert decide_prearm("A", None, 0.9, 0.2) is None
    assert decide_prearm("A", "A", 0.9, 0.2) is None


def test_missing_current_no_op():
    """读不到当前值（设备已消失等）→ 不动。"""
    assert decide_prearm("A", "B", None, 0.2) is None


def test_prearm_cooldown():
    """同设备布防冷却（see-saw 防护）：冷却期内拒绝，期满放行。"""
    from knowbot.executors.volume import prearm_allowed
    assert prearm_allowed(None, 100.0) is True            # 从未布防
    assert prearm_allowed(100.0, 100.0 + 299.0) is False  # 冷却期内
    assert prearm_allowed(100.0, 100.0 + 300.0) is True   # 期满
