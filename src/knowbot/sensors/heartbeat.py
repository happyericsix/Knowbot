"""心跳：存活证明 + 事件缺口探测器。启动立即打一拍。"""
from __future__ import annotations

import asyncio
import os

from ..bus import EventBus
from ..envelope import Event
from .base import Sensor


class HeartbeatSensor(Sensor):
    name = "system.heartbeat"

    def __init__(self, bus: EventBus, period: float = 60.0) -> None:
        self._bus = bus
        self._period = period
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            self._bus.publish(Event(self.name, "beat", {"pid": os.getpid()}))
            await asyncio.sleep(self._period)

    def start(self, loop) -> None:
        self._task = loop.create_task(self._run(), name="sensor-heartbeat")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
