"""daemon 装配：总线 → 落库 → 传感器 → 优雅退出。

Phase 0 纪律：无决策层、无动作（连 pre-arm 都不碰）、无面板、无模型。
传感器缺依赖或启动失败只降级为 unavailable 事件，绝不拖垮 daemon。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from . import __version__
from .bus import EventBus
from .config import Config
from .envelope import Event, Sensor as SensorNS, now_wall
from .store.db import EventStore
from .sensors.audio_device import AudioDeviceSensor
from .sensors.base import Sensor
from .sensors.heartbeat import HeartbeatSensor
from .sensors.winmsg import Win32MessageSensor

log = logging.getLogger(__name__)


async def run(config: Config, duration: float = 0.0) -> dict:
    loop = asyncio.get_running_loop()
    bus = EventBus(maxsize=config.queue_size)
    bus.bind(loop)
    store = EventStore(config.db_path, batch_size=config.batch_size,
                       flush_interval=config.flush_interval)
    bus.subscribe("", store.append)  # 全命名订阅，唯一落库通道

    sensors: list[Sensor] = []

    def add(sensor: Sensor) -> None:
        try:
            sensor.start(loop)
            sensors.append(sensor)
        except Exception:
            log.exception("传感器启动失败：%s", sensor.name)

    add(HeartbeatSensor(bus, period=config.heartbeat_period))
    if config.enable_audio:
        add(AudioDeviceSensor(bus))
    if config.enable_winmsg:
        add(Win32MessageSensor(bus))

    store.set_meta("last_start", now_wall())
    store.set_meta("version", __version__)
    bus.publish(Event(SensorNS.DAEMON, "start", {
        "pid": os.getpid(),
        "version": __version__,
        "python": sys.version.split()[0],
        "sensors": [s.name for s in sensors],
        "db_path": str(config.db_path),
    }))

    tasks = [
        loop.create_task(bus.run(), name="bus"),
        loop.create_task(store.run(), name="store-flush"),
    ]
    stop = asyncio.Event()
    if duration > 0:
        async def _duration_timer():
            await asyncio.sleep(duration)
            stop.set()
        tasks.append(loop.create_task(_duration_timer(), name="duration"))

    def _on_signal():
        log.info("收到停止信号")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGINT)):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            pass  # Windows 对部分信号不支持，Ctrl+C 由 KeyboardInterrupt 兜底

    await stop.wait()

    # ---- 优雅退出：先停任务，再停传感器线程，最后补记 stop 事件并冲刷 ----
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    for s in reversed(sensors):
        try:
            s.stop()
        except Exception:
            log.exception("传感器停止失败：%s", s.name)

    summary = {
        "published": bus.published,
        "dropped": bus.dropped,
        "flushed": store.flushed,
        "uptime_hint": True,
    }
    # 总线已停，stop 事件直接写库
    store.append(Event(SensorNS.DAEMON, "stop", summary))
    store.set_meta("last_stop", now_wall())
    store.close()
    log.info("daemon 退出：%s", summary)
    return summary
