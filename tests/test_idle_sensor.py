"""空闲传感器测试：转换判定纯函数 + 事件序列（注入 idle 源，零 Win32 依赖）。"""
import asyncio

from knowbot.bus import EventBus
from knowbot.sensors.idle import IdleSensor, idle_step


def test_idle_step_transitions():
    t = 300_000  # 300s 阈值
    assert idle_step(False, 100_000, t) == (False, None)    # 活跃持续：无事件
    assert idle_step(False, 300_000, t) == (True, "idle_begin")   # 越过阈值
    assert idle_step(True, 400_000, t) == (True, None)      # 空闲持续：无事件
    assert idle_step(True, 299_999, t) == (False, "idle_end")     # 活动恢复
    assert idle_step(False, 5_000, t) == (False, None)      # 活跃持续


def test_sensor_publishes_begin_then_end_with_duration():
    """越阈值 → idle_begin；恢复 → idle_end 带整段时长（恢复前一拍观测值）。"""
    async def main():
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        events = []
        bus.subscribe("system.idle", events.append)
        runner = asyncio.create_task(bus.run())   # 订阅回调在分发循环里触发
        seq = [400_000, 410_000, 5_000, 3_000]  # ms：空闲→空闲→恢复→活跃
        sensor = IdleSensor(bus, threshold_s=300.0, poll_s=0.02,
                            idle_fn=lambda: seq.pop(0) if seq else 3_000)
        sensor.start(loop)
        await asyncio.sleep(0.2)
        sensor.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

        assert [(e.type, e.payload) for e in events] == [
            ("idle_begin", {"idle_s": 400.0}),
            ("idle_end", {"duration_s": 410.0}),   # 整段时长，非本拍 5s
        ]

    asyncio.run(main())


def test_sensor_quiet_when_never_idle():
    async def main():
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        events = []
        bus.subscribe("system.idle", events.append)
        runner = asyncio.create_task(bus.run())
        sensor = IdleSensor(bus, threshold_s=300.0, poll_s=0.02,
                            idle_fn=lambda: 60_000)  # 一直只空闲 1 分钟
        sensor.start(loop)
        await asyncio.sleep(0.12)
        sensor.stop()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        assert events == []

    asyncio.run(main())
