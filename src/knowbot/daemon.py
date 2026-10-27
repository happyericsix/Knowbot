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
from .aggregation.audio import AudioAggregationService, AudioFoldParams
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
        add(AudioDeviceSensor(
            bus,
            watch_volume=config.enable_volume_watch,
            vol_settle_s=config.vol_settle_s,
            enable_prearm=config.enable_prearm,
            prearm_safe_scalar=config.prearm_safe_scalar,
            prearm_only_lower=config.prearm_only_lower,
        ))
    if config.enable_winmsg:
        add(Win32MessageSensor(bus))

    aggregation = None
    if config.enable_aggregation:
        aggregation = AudioAggregationService(
            bus,
            AudioFoldParams(settle_s=config.agg_settle_s, churn_flips=config.agg_churn_flips,
                            max_span_s=config.agg_max_span_s),
            tick_interval=config.agg_tick_interval,
        )
        aggregation.start(loop)

    # Phase 1 ②：音量技能（首个 Skill，dry-run 影子模式）
    skill = None
    panel = None
    volume_executor = None
    if config.enable_volume_skill or config.enable_panel:
        from .attribution.volume import VolumeAttributionParams
        from .executors.volume import VolumeExecutor
        from .store.memory import MemoryStore, seed_defaults

        memory = MemoryStore(store._conn)
        seed_defaults(memory)
        volume_executor = VolumeExecutor()

    if config.enable_volume_skill:
        from .learning.learner import VolumeLearner
        from .skills.volume import VolumeSkill

        notifier = None
        if config.enable_toast:
            from .notify.toast import ensure_appid, toast as _toast
            ensure_appid()  # 未注册 AUMID 的 toast 静默不显示（真机实测坑）
            notifier = _toast
            try:  # toast 三键按钮的回传通道；失败仅降级为无按钮 toast
                from .notify.protocol import ensure_protocol_handler
                ensure_protocol_handler(config.db_path)
            except Exception:
                log.warning("knowbot: 协议注册失败（校准按钮不可用）", exc_info=True)
        skill = VolumeSkill(
            bus, memory, VolumeLearner(memory), volume_executor,
            VolumeAttributionParams(), dry_run=config.volume_skill_dry_run,
            notifier=notifier)
        skill.start(loop)

    if config.enable_panel:
        from .panel.server import PanelServer
        panel = PanelServer(
            config.db_path, executor=volume_executor, port=config.panel_port,
            flags={"dry_run": config.volume_skill_dry_run})
        panel.start()

    store.set_meta("last_start", now_wall())
    store.set_meta("version", __version__)
    bus.publish(Event(SensorNS.DAEMON, "start", {
        "pid": os.getpid(),
        "version": __version__,
        "python": sys.version.split()[0],
        "sensors": [s.name for s in sensors],
        "aggregation": config.enable_aggregation,
        "volume_skill": config.enable_volume_skill,
        "volume_skill_dry_run": config.volume_skill_dry_run,
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

    def _request_stop(sig=None, frame=None):
        log.info("收到停止信号%s", f" ({sig})" if sig else "")
        # 信号处理器在主线程执行；经 call_soon_threadsafe 唤醒事件循环。
        # Windows 上 asyncio 的 add_signal_handler 不可用（Proactor 循环），
        # 此前靠 KeyboardInterrupt 裸退——stop 事件丢失，会话边界不完整。
        loop.call_soon_threadsafe(stop.set)

    # Windows：SIGINT(Ctrl+C)/SIGBREAK(Ctrl+Break) 用 signal.signal 注册
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass  # 非主线程等场景；duration 路径仍可用

    await stop.wait()

    # ---- 优雅退出：先停任务，再停聚合与传感器线程，最后补记 stop 事件并冲刷 ----
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if aggregation is not None:
        aggregation.stop()
    if skill is not None:
        skill.stop()
    if panel is not None:
        panel.stop()

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
