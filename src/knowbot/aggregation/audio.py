"""A3 音频事件聚合层：把同一端点的状态爆发折叠成一条语义事件。

问题（真实语料，2026-10-25 晨）：显示器唤醒时 NVIDIA 音频端点
0.67 秒内翻转 6 次、89 秒后再翻 5 次——原始流 11 条 state_changed。
没有聚合层，Phase 1 音量技能会被这种抖动反复触发。

架构定位（快慢分离的落实）：
- 聚合器服务**慢路径**与学习统计；
- 快反射层（pre-arm）直接订阅原始 audio.device 事件——
  settle 延迟只影响"定案"，不影响"出手"。
- 产出 audio.flow 命名空间的派生事件（payload 带 derived:true），
  与原始事件同总线、同库、同回放语料。

规则（docs/phase1-event-aggregation.md）：
- state_changed / device_added 按设备缓冲，安静 settle_s 即定案为
  device_settled（final_state + flips + span_ms + churn 标记）；
- device_removed 是定案事件，立即折叠出 settled；
- default_changed 按 (flow, role) 缓冲，同设备连续重复折叠为一条
  default_output（repeats 计数），换设备即切换窗口；
- max_span_s 安全阀强制定案，防缓冲永悬；
- snapshot 是基线数据，不折叠。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

from ..bus import EventBus
from ..envelope import Event


@dataclass(frozen=True)
class AudioFoldParams:
    settle_s: float = 2.0       # 爆发后安静这么久即定案
    max_span_s: float = 60.0    # 安全阀：窗口最长跨度
    churn_flips: int = 3        # 单设备翻转 ≥ 此值 → churn
    co_window_s: float = 3.0    # 协同窗口：此窗内多设备同翻 = 系统级抖动
    co_devices: int = 2         # 协同设备数 ≥ 此值 → churn（真实语料修正：
                                # 显示器唤醒 = 3 端点各翻 2 次，单设备阈值抓不住）


def _meta_of(payload: dict) -> dict:
    return {k: payload[k] for k in ("name", "form_factor", "flow") if k in payload}


class AudioDeviceFolder:
    """纯函数折叠器：feed(原始事件, 时钟) → 语义事件；无 I/O、无隐藏时钟。"""

    def __init__(self, params: AudioFoldParams | None = None) -> None:
        self.p = params or AudioFoldParams()
        self._states: dict[str, dict] = {}     # device_id → 缓冲
        self._defaults: dict[tuple, dict] = {}  # (flow, role) → 缓冲
        # 翻转时间线：容量界定的滑动窗口。不按"当前时间"修剪——
        # 晚到的定案（如两段爆发间隔 89 秒）仍需锚点附近的历史算协同数。
        self._recent: deque[tuple[float, str]] = deque(maxlen=256)

    def feed(self, ev: Event, now: float) -> list[Event]:
        outs = self.tick(now)  # 每次喂入先做过期检查（其他设备的窗口可能已到定案时间）
        if ev.sensor != "audio.device" or ev.type in ("snapshot", "unavailable"):
            return outs
        if ev.type == "default_changed":
            return outs + self._feed_default(ev)
        if ev.type in ("state_changed", "device_added", "device_removed"):
            return outs + self._feed_state(ev)
        return outs

    def tick(self, now: float) -> list[Event]:
        """推进时钟：到期窗口定案。服务层周期调用（真实时钟）或测试注入。"""
        outs = []
        expired = [k for k, b in self._states.items() if now - b["last_mono"] >= self.p.settle_s]
        for did in expired:
            outs.append(self._emit_settled(did, self._states.pop(did), now))
        expired = [k for k, b in self._defaults.items() if now - b["last_mono"] >= self.p.settle_s]
        for key in expired:
            outs.append(self._emit_default(self._defaults.pop(key), now))
        return outs

    # ---- 状态折叠 ----

    def _feed_state(self, ev: Event) -> list[Event]:
        did = ev.payload.get("device_id")
        if not did:
            return []
        self._recent.append((ev.ts_mono, did))
        b = self._states.get(did)
        if b is None:
            b = {"flips": 0, "first_mono": ev.ts_mono, "last_mono": ev.ts_mono,
                 "last_wall": ev.ts_wall, "meta": {}, "final": None}
            self._states[did] = b
        b["flips"] += 1
        b["last_mono"] = ev.ts_mono
        b["last_wall"] = ev.ts_wall
        b["meta"].update(_meta_of(ev.payload))

        if ev.type == "state_changed":
            b["final"] = ev.payload.get("state")
        elif ev.type == "device_added":
            b["final"] = b["final"] or "added"
        else:  # device_removed：定案事件，立即出结果
            b["final"] = "removed"
            return [self._emit_settled(did, self._states.pop(did), ev.ts_mono)]

        if ev.ts_mono - b["first_mono"] >= self.p.max_span_s:  # 安全阀
            return [self._emit_settled(did, self._states.pop(did), ev.ts_mono)]
        return []

    def _emit_settled(self, did: str, b: dict, now: float) -> Event:
        # 协同指纹：以该设备最后一次翻转为锚，窗口内还有哪些别的设备在翻
        # （正常插拔无邻居；显示器唤醒/驱动重置 = 多端点同步翻。）
        anchor = b["last_mono"]
        co = len({d for ts, d in self._recent
                  if d != did and abs(ts - anchor) <= self.p.co_window_s})
        churn = b["flips"] >= self.p.churn_flips or co >= self.p.co_devices
        return Event("audio.flow", "device_settled", {
            "device_id": did, **b["meta"],
            "final_state": b["final"] or "unknown",
            "flips": b["flips"],
            "span_ms": round((b["last_mono"] - b["first_mono"]) * 1000, 1),
            "co_flipping": co,
            "churn": churn,
            "derived": True,
        }, ts_mono=now, ts_wall=b["last_wall"])

    # ---- 默认输出折叠 ----

    def _feed_default(self, ev: Event) -> list[Event]:
        p = ev.payload
        key = (p.get("flow"), p.get("role"))
        b = self._defaults.get(key)
        if b is not None and b["device_id"] == p.get("device_id"):
            b["repeats"] += 1
            b["last_mono"] = ev.ts_mono
            b["last_wall"] = ev.ts_wall
            b["meta"].update(_meta_of(p))
            return []
        outs = []
        if b is not None:  # 同角色换了设备：旧结论即定案
            outs.append(self._emit_default(self._defaults.pop(key), ev.ts_mono))
        self._defaults[key] = {
            "flow": key[0], "role": key[1], "device_id": p.get("device_id"),
            "repeats": 1, "first_mono": ev.ts_mono, "last_mono": ev.ts_mono,
            "last_wall": ev.ts_wall, "meta": _meta_of(p),
        }
        return outs

    def _emit_default(self, b: dict, now: float) -> Event:
        return Event("audio.flow", "default_output", {
            "flow": b["flow"], "role": b["role"], "device_id": b["device_id"],
            **b["meta"], "repeats": b["repeats"],
            "span_ms": round((b["last_mono"] - b["first_mono"]) * 1000, 1),
            "derived": True,
        }, ts_mono=now, ts_wall=b["last_wall"])


class AudioAggregationService:
    """总线接线：订阅 audio.device → 折叠 → 发布 audio.flow（随 store 落库）。"""

    def __init__(self, bus: EventBus, params: AudioFoldParams | None = None,
                 tick_interval: float = 0.5) -> None:
        self._bus = bus
        self._folder = AudioDeviceFolder(params)
        self._tick_interval = tick_interval
        self._task: asyncio.Task | None = None

    def start(self, loop) -> None:
        self._bus.subscribe("audio.device", self._on_raw)
        self._task = loop.create_task(self._run(), name="agg-audio")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval)
            for ev in self._folder.tick(time.monotonic()):
                self._bus.publish(ev)

    def _on_raw(self, ev: Event) -> None:
        for sem in self._folder.feed(ev, time.monotonic()):
            self._bus.publish(sem)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
