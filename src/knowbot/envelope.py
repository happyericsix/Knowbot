"""事件信封 —— 全项目唯一的事件表示（契约，改动需升级 schema 版本）。

约定：
- seq：进程内单调递增（itertools.count 在 CPython 下原子）；重启归零，
  跨进程时序以 ts_wall + 落库自增 id 为准。
- ts_mono：time.monotonic() 秒。排序与延迟测量的唯一基准，永不用于人读。
- ts_wall：本地时区 ISO8601（毫秒精度）。仅供人读与跨进程对账。
- payload 只存设备/应用标识，永不存内容——隐私红线做进 schema。
"""
from __future__ import annotations

import datetime
import itertools
import json
import time
from dataclasses import dataclass, field

_seq = itertools.count(1)


class Sensor:
    """传感器命名空间：`域.子域`，总线按前缀匹配订阅。"""

    HEARTBEAT = "system.heartbeat"
    DAEMON = "system.daemon"
    CLOCK = "system.clock_reconcile"
    SENSOR_STATUS = "system.sensor"
    POWER = "system.power"
    SESSION = "system.session"
    IDLE = "system.idle"
    AUDIO = "audio.device"
    FOREGROUND = "app.foreground"
    USB = "device.usb"
    VOLUME = "audio.volume"


def now_wall() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass(slots=True)
class Event:
    sensor: str
    type: str
    payload: dict
    seq: int = field(default_factory=lambda: next(_seq))
    ts_mono: float = field(default_factory=time.monotonic)
    ts_wall: str = field(default_factory=now_wall)

    def to_row(self) -> tuple:
        """event_raw 表一行（不含自增 id）。"""
        return (
            self.seq,
            self.ts_mono,
            self.ts_wall,
            self.sensor,
            self.type,
            json.dumps(self.payload, ensure_ascii=False),
        )

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "ts_mono": self.ts_mono,
            "ts_wall": self.ts_wall,
            "sensor": self.sensor,
            "type": self.type,
            "payload": self.payload,
        }

    @classmethod
    def from_json(cls, obj: dict) -> "Event":
        return cls(
            sensor=obj["sensor"],
            type=obj["type"],
            payload=obj.get("payload", {}),
            seq=obj["seq"],
            ts_mono=obj["ts_mono"],
            ts_wall=obj["ts_wall"],
        )
