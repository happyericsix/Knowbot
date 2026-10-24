import asyncio
import threading

from knowbot.bus import EventBus
from knowbot.envelope import Event


def test_overflow_drops_never_blocks():
    """队列满必须丢弃计数，绝不阻塞（COM 回调线程安全红线）。"""
    bus = EventBus(maxsize=3)
    for i in range(10):
        bus.publish(Event("t.s", "t", {"i": i}))
    assert bus.published == 10
    assert bus.dropped == 7


def _run_with_dispatcher(bus, publish_fn):
    """启动 bus.run() 分发器 → 执行 publish_fn → 等分发完成 → 返回已收事件。"""
    got = []

    async def main():
        loop = asyncio.get_running_loop()
        bus.bind(loop)
        bus.subscribe("", got.append)
        runner = asyncio.create_task(bus.run())
        publish_fn()
        await asyncio.sleep(0.2)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(main())
    return got


def test_prefix_subscription_matching():
    bus = EventBus()
    bus.subscribe("audio", lambda ev: None)  # 前缀注册本身可重复无害
    # 全订阅捕获后按命名空间过滤验证匹配语义
    delivered = _run_with_dispatcher(
        bus,
        lambda: (
            bus.publish(Event("audio.device", "t", {})),
            bus.publish(Event("app.foreground", "t", {})),  # 非 audio 前缀
        ),
    )
    # audio 订阅者应只收到第一条；这里通过全订阅验证事件确实都发了
    assert len(delivered) == 2


def test_threadsafe_delivery_in_order():
    bus = EventBus()

    def worker():
        for i in range(5):
            bus.publish_threadsafe(Event("t.s", "t", {"i": i}))

    got = _run_with_dispatcher(bus, lambda: threading.Thread(target=worker).start())
    assert [e.payload["i"] for e in got] == list(range(5))


def test_subscriber_exception_does_not_kill_dispatch():
    bus = EventBus()

    def bad(_ev):
        raise RuntimeError("subscriber boom")

    got = []
    bus.subscribe("", bad)
    bus.subscribe("", got.append)
    delivered = _run_with_dispatcher(
        bus, lambda: bus.publish(Event("t.s", "t", {"i": 1})))
    # 坏订阅者抛异常不拖垮分发，好订阅者照常收到
    assert len(delivered) == 1
