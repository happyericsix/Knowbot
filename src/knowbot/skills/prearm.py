"""pre-arm 台账落账：反射层动作的唯一审计入口（哲学 7：台账不可豁免）。

pre-arm 在传感器反射层执行（快路径、纯规则、无模型），本模块只负责
事后落账：skill=prearm / exec_mode=silent / risk=zero / undo=前值 /
溯源到触发它的 default_changed 事件。

反应位留空（无观察窗）：prearm 对用户不可见，纠正信号 = 事后撤销
（面板，撤销即 reject/false_alarm）或下次使用该设备时的手动调整
（由 audio.volume 观察链路自然捕获）——不虚构裁决。
"""
from __future__ import annotations

import logging

from ..bus import EventBus
from ..envelope import Event
from ..store.memory import MemoryStore

log = logging.getLogger(__name__)


class PrearmLedger:
    name = "prearm"

    def __init__(self, bus: EventBus, memory: MemoryStore,
                 backfill_delay_s: float = 2.0) -> None:
        self._bus = bus
        self._m = memory
        self._backfill_delay_s = backfill_delay_s
        self._loop = None

    def start(self, loop) -> None:
        self._loop = loop
        self._bus.subscribe("audio.device", self._on_prearm)

    def stop(self) -> None:
        pass  # 订阅随总线生命周期结束

    def _on_prearm(self, ev: Event) -> None:
        if ev.type != "prearm":
            return
        p = ev.payload
        action_id = self._m.log_action(
            self.name, None, None,
            {"type": "prearm", "device_id": p.get("device_id"),
             "before": p.get("before"), "after": p.get("after")},
            "silent", ev.ts_mono, risk="zero",
            undo_payload={"restore": p.get("before")})
        log.info("pre-arm 台账：#%s %s %s→%s", action_id,
                 (p.get("device_id") or "")[-12:], p.get("before"), p.get("after"))
        # 触发事件（default_changed）此刻多半还在落库缓冲区，延迟反查回填
        if self._loop is not None and p.get("new_default"):
            self._loop.call_later(
                self._backfill_delay_s, self._backfill,
                action_id, p["new_default"], ev.ts_mono)

    def _backfill(self, action_id: int, new_default: str, ts_mono: float) -> None:
        try:
            tid = self._m.find_event_id(
                "audio.device", "default_changed", new_default, ts_mono,
                tol_s=10.0)
            if tid is not None:
                self._m.set_action_trigger(action_id, tid)
        except Exception:
            log.exception("pre-arm 溯源回填失败 action=%s", action_id)
