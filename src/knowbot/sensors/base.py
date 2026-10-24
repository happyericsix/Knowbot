"""Sensor 协议：三个方法，抽象等第二个使用者再长。"""
from __future__ import annotations

import abc


class Sensor(abc.ABC):
    #: 命名空间，见 envelope.Sensor
    name: str = "sensor.unknown"

    @abc.abstractmethod
    def start(self, loop) -> None:
        """启动（可阻塞至线程/任务就绪）；失败应自行发布 unavailable 事件。"""

    @abc.abstractmethod
    def stop(self) -> None:
        """优雅停止，最多阻塞数秒。"""
