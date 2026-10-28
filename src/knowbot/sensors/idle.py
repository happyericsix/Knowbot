"""空闲传感器：GetLastInputInfo 轮询 → system.idle 事件流。

隐私红线：只采"距上次键鼠输入过了多久"这一个数字，永不接触输入内容。

事件（纯转换，无周期心跳）：
- idle_begin：空闲越过阈值（默认 5 分钟）；
- idle_end：活动恢复，payload 带整段空闲时长（取恢复前一拍的观测值）。

用途（v1 记录，消费方后续接入）：节律记忆（Phase 3 的"你通常几点收尾"）、
计算礼貌（空闲窗口推理）、missed_need 检测增强。
轮询为单次轻量 API 调用（默认 10s 一拍），asyncio 任务承载。
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
from ctypes import wintypes

from ..bus import EventBus
from ..envelope import Event
from .base import Sensor

log = logging.getLogger(__name__)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def idle_ms() -> int:
    """距上一次键鼠输入的毫秒数（系统级）。读失败按活跃（0）处理。"""
    lii = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO))
    if not _user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0
    return (_kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF  # 回绕安全


def idle_step(idle_state: bool, idle_ms_now: int, threshold_ms: int):
    """纯转换判定：(当前是否空闲, 本拍空闲毫秒, 阈值) → (新状态, 事件|None)。"""
    above = idle_ms_now >= threshold_ms
    if above and not idle_state:
        return True, "idle_begin"
    if not above and idle_state:
        return False, "idle_end"
    return idle_state, None


class IdleSensor(Sensor):
    name = "system.idle"

    def __init__(self, bus: EventBus, threshold_s: float = 300.0,
                 poll_s: float = 10.0, idle_fn=None) -> None:
        self._bus = bus
        self._threshold_ms = int(threshold_s * 1000)
        self._poll_s = poll_s
        self._idle_fn = idle_fn or idle_ms  # 测试注入
        self._idle = False
        self._last_ms = 0
        self._task: asyncio.Task | None = None

    def start(self, loop) -> None:
        self._task = loop.create_task(self._run(), name="sensor-idle")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                ms = int(self._idle_fn())
                new_idle, ev = idle_step(self._idle, ms, self._threshold_ms)
                if ev == "idle_begin":
                    self._publish("idle_begin", {"idle_s": round(ms / 1000, 1)})
                elif ev == "idle_end":
                    # 整段时长 = 恢复前一拍的观测值（本拍已回到活跃）
                    self._publish("idle_end",
                                  {"duration_s": round(self._last_ms / 1000, 1)})
                self._idle = new_idle
                self._last_ms = ms
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("空闲传感器 tick 失败")
            await asyncio.sleep(self._poll_s)

    def _publish(self, etype: str, payload: dict) -> None:
        self._bus.publish(Event(self.name, etype, payload))
