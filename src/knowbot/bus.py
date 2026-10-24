"""事件总线：一条有界 asyncio 队列 + 前缀订阅分发。

硬规则：队列满时丢弃并计数，绝不反压——阻塞 COM 回调线程可能
拖死系统音频通知。丢弃计数在 daemon 退出摘要里可见。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from .envelope import Event

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, maxsize: int = 10_000) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs: dict[str, list] = defaultdict(list)  # prefix -> [callback]
        self._loop: asyncio.AbstractEventLoop | None = None
        self.published = 0
        self.dropped = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish_threadsafe(self, event: Event) -> None:
        """传感器线程调用（事件循环线程外）。"""
        if self._loop is None:
            raise RuntimeError("EventBus.bind(loop) 未调用")
        self._loop.call_soon_threadsafe(self.publish, event)

    def publish(self, event: Event) -> None:
        """事件循环线程内调用（或经 call_soon_threadsafe 转入）。"""
        self.published += 1
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1  # 溢出即丢，绝不反压

    def subscribe(self, prefix: str, callback) -> None:
        """prefix 为空串 = 订阅全部命名空间。callback 为同步函数，在循环线程执行。"""
        self._subs[prefix].append(callback)

    async def run(self) -> None:
        while True:
            event = await self._queue.get()
            for prefix, callbacks in self._subs.items():
                if prefix == "" or event.sensor == prefix or event.sensor.startswith(prefix + "."):
                    for cb in callbacks:
                        try:
                            cb(event)
                        except Exception:
                            log.exception("订阅者处理事件失败 sensor=%s type=%s", event.sensor, event.type)
