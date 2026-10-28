"""音量技能：第一个真实 Skill（触发/查询/执行/观察/学习闭环）。

触发（2026-10-27 修订，design-amendments §13）：**成为听音输出的一切端点**——
    插拔定案或手动切默认皆可，form_factor 只用于选类级先验，不再做闸门；
    仅排除 hdmi（显示器/电视，含 DP）与 remote_network_device（远程桌面
    虚拟端点）——它们的到达不是"个人听音"意图。
    实证：ZhuAudio ZG2 USB DAC 接耳机，Windows 报 speakers，耳机类白名单漏掉。
查询：作用域链 exception > instance > class > global（A1 §5）。
执行：置信度分级（≥0.85 静默 / ≥0.70 通知 / ≥0.50 建议 / 无信念 none），
      dry-run 全局开关优先；全部进台账（不可豁免）。
观察：执行/决策后开观察窗（A2 参数），窗口内收集去抖后的音量调整
      （actor=user）+ 电平采样，窗口结束跑归因 → 学习器应用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from ..attribution.volume import (
    AgentAction, MeterStats, UserAdjustment, VolumeAttributionParams, VolumeCase,
    attribute,
)
from ..bus import EventBus
from ..envelope import Event
from ..learning.learner import VolumeLearner
from ..notify.protocol import calibrate_uri
from ..store.memory import MemoryStore

log = logging.getLogger(__name__)

# 触发排除的 form_factor：显示器/电视音频（hdmi 含 DP——churn 风暴源头）
# 与远程桌面虚拟端点。缺 form_factor（元信息读取失败）同样不触发。
TRIGGER_EXCLUDED_FORM_FACTORS = {"hdmi", "remote_network_device"}

# 执行分级阈值（config 化留待 ③；与 A1 §3 一致）
CONF_SILENT = 0.85
CONF_NOTIFY = 0.70
CONF_SUGGEST = 0.50

# 触发去重窗：插拔会同时产生 device_settled 与 default_output（两条语义事件
# 指向同一意图），窗口内的第二个触发让路——对意图反应，不对物理动作重复反应
TRIGGER_DEDUP_S = 5.0

# 影子决策 toast 节流（哲学 9 计算礼貌；决策照常落台账，只压制通知）：
# 同设备冷却窗 + 全局日预算。真实执行档（notify/silent）不受此限。
SHADOW_TOAST_COOLDOWN_S = 600.0
SHADOW_TOAST_DAILY = 30

class VolumeSkill:
    name = "volume"

    def __init__(self, bus: EventBus, memory: MemoryStore, learner: VolumeLearner,
                 executor, params: VolumeAttributionParams | None = None,
                 dry_run: bool = True, meter_factory=None,
                 notifier=None, toast_cooldown_s: float = SHADOW_TOAST_COOLDOWN_S,
                 toast_daily: int = SHADOW_TOAST_DAILY) -> None:
        self._bus = bus
        self._m = memory
        self._learner = learner
        self._executor = executor          # get(device_id)/set(device_id, value)->before
        self._p = params or VolumeAttributionParams()
        self._dry_run = dry_run
        self._meter_factory = meter_factory  # () -> 采样器（测试注入 None 跳过）
        self._notifier = notifier           # (title, body) -> None（默认 toast）
        self._toast_cooldown_s = toast_cooldown_s
        self._toast_daily = toast_daily
        self._toast_ts: dict[str, float] = {}      # device_id → 上次影子 toast
        self._toast_day: tuple[str, int] = ("", 0)  # (日期, 当日已用额度)
        self._watches: dict[str, _Watch] = {}
        self._last_trigger: dict[str, float] = {}  # device_id → 触发时刻（去重）
        self._pending: dict[str, Event] = {}   # 观察窗占用期被挡的触发（停轮后补决策）
        self._current_default: str | None = None  # 原始 default_changed 维护的当前默认（毫秒级真相）
        self._task: asyncio.Task | None = None

    def start(self, loop) -> None:
        self._bus.subscribe("audio.flow", self._on_flow)
        self._bus.subscribe("audio.volume", self._on_volume)
        self._bus.subscribe("audio.device", self._on_raw_default)
        self._task = loop.create_task(self._run_expiry(), name="skill-volume")

    def _on_raw_default(self, ev: Event) -> None:
        """原始 default_changed：维护"当前默认听音输出"的毫秒级真相。
        折叠事件滞后 1s+，用它做时效校验会放行过期决策（2026-10-27 实测：
        切走后 toast 仍在说旧设备）。"""
        if ev.type != "default_changed":
            return
        p = ev.payload
        if p.get("flow") == "render" and p.get("role") == "multimedia" and p.get("device_id"):
            self._current_default = p["device_id"]

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run_expiry(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            for dev, w in list(self._watches.items()):
                if now >= w.expires_at:
                    del self._watches[dev]
                    try:
                        self._finalize(w)
                    except Exception:
                        log.exception("观察窗口收尾失败 device=%s", dev)
            # 补决策：连续切换被观察窗挡掉的触发，若你仍停在该输出上，现在补一次
            # （2026-10-27 实测缺口：快速来回切换的最终停留输出可能永远无决策）
            for dev, ev in list(self._pending.items()):
                if dev in self._watches:
                    continue
                if (self._current_default is not None
                        and dev != self._current_default):
                    del self._pending[dev]  # 你已停在别处：过期意图作废
                    continue
                del self._pending[dev]
                try:
                    self._handle_activation(ev)
                except Exception:
                    log.exception("补决策失败 device=%s", dev)

    # ---------- 触发 ----------

    def _on_flow(self, ev: Event) -> None:
        if ev.type == "device_settled":
            # 路径一：插拔定案（设备从未激活 → 激活）
            p = ev.payload
            ff = p.get("form_factor")
            did = p.get("device_id") or ""
            # capture 端点不是听音输出（2026-10-27 首次真实触发实测：ZG2 麦克风
            # 曾误触）。state_changed 元信息不含 flow，按 MMDevice ID 前缀识别：
            # capture 端点为 {0.0.1.00000000}.…，render 为 {0.0.0.00000000}.…
            flow = p.get("flow") or ("capture" if did.startswith("{0.0.1.") else None)
            if (p.get("final_state") != "active" or p.get("churn")
                    or not ff or ff in TRIGGER_EXCLUDED_FORM_FACTORS
                    or ff == "microphone" or flow == "capture"):
                return
        elif ev.type == "default_output":
            # 路径二：手动切换默认输出（设备已激活、只换了指向——意图等价）。
            # 此路不受 churn 标记影响（显示器风暴不改默认输出的实测依据）。
            p = ev.payload
            ff = p.get("form_factor")
            if (p.get("flow") != "render" or p.get("role") != "multimedia"
                    or not p.get("device_id")
                    or not ff or ff in TRIGGER_EXCLUDED_FORM_FACTORS
                    or ff == "microphone"):
                return
            last = self._last_trigger.get(p["device_id"])
            if last is not None and time.monotonic() - last < TRIGGER_DEDUP_S:
                return  # 插拔链已由 device_settled 触发，让路
        else:
            return
        try:
            self._handle_activation(ev)
        except Exception:
            log.exception("音量技能处理激活失败")

    def _handle_activation(self, ev: Event) -> None:
        if self._m.get_meta("paused") == "1":
            log.info("全局暂停中，音量技能不出手")
            return
        p = ev.payload
        device_id = p["device_id"]
        # 时效校验：触发折叠有 ~1s 滞后，决策瞬间若默认输出已再换，
        # 这是一次过期触发——静默作废（对停留意图决策，不对过期动作反应）。
        if (self._current_default is not None
                and device_id != self._current_default):
            log.info("触发过期（当前默认已换），跳过 device=%s", device_id[-12:])
            return
        if device_id in self._watches:
            self._pending[device_id] = ev  # 观察窗占用中：停轮后由 expiry 补最终决策
            return
        self._last_trigger[device_id] = time.monotonic()
        trigger = (ev.sensor, ev.type, device_id, ev.ts_mono)  # finalize 反查 event_raw.id 用
        inst_key = f"audio.device:{device_id}"
        class_key = f"audio.form_factor:{p['form_factor']}"
        label = p.get("name") or device_id[-12:]  # toast 上人读的设备名（缺则退 GUID 尾巴）

        rows = self._m.resolve("audio.volume", inst_key, class_key)
        pref = rows[0] if rows else None
        conf = float(pref["confidence"]) if pref else 0.0

        if pref is None or conf < CONF_SUGGEST:
            mode = "none"
        elif conf >= CONF_SILENT:
            mode = "silent"
        elif conf >= CONF_NOTIFY:
            mode = "notify"
        else:
            mode = "suggest"

        action_ts = time.monotonic()

        if mode == "none":
            action_id = self._m.log_action(
                self.name, None, pref["id"] if pref else None,
                {"reason": "no_belief", "resolved_conf": conf}, "none", action_ts)
            self._open_watch(device_id, inst_key, class_key, action_id,
                             pref["id"] if pref else None, action_ts,
                             agent=None, window_s=self._p.c_window_s, trigger=trigger)
            log.info("音量技能：无适用信念（conf=%.2f），开 missed-need 观察窗", conf)
            return

        target = json.loads(pref["value"]).get("scalar")
        if target is None:
            log.warning("信念值缺 scalar：%s", pref["value"])
            return

        effective_mode = "dry_run" if self._dry_run else mode
        if effective_mode == "dry_run":
            before = self._executor.get(device_id)
            action_id = self._m.log_action(
                self.name, None, pref["id"],
                {"type": "set_volume", "device_id": device_id,
                 "before": before, "after": target, "would_mode": mode},
                "dry_run", action_ts,
                undo_payload={"restore": before},
                latency_ms=(time.monotonic() - action_ts) * 1000)
            # 影子模式 toast 即交互本体：三键校准是影子周决策质量的显式裁决通道
            # （节流：同设备冷却 + 日预算——快速切换风暴不再连环轰炸，台账照记）
            self._shadow_notify(
                device_id, "knowbot 影子决策",
                f"我会把 {label} 调到 {target:.0%}——感觉如何？",
                self._calibrate_actions(pref["id"], action_id))
        elif effective_mode == "suggest":
            before = self._executor.get(device_id)
            action_id = self._m.log_action(
                self.name, None, pref["id"],
                {"type": "suggest", "device_id": device_id, "target": target},
                "suggest", action_ts)
            self._notify("knowbot 建议",
                         f"建议把音量调到 {target:.0%}——需要我以后自动来吗？",
                         self._calibrate_actions(pref["id"], action_id))
        else:
            t0 = time.monotonic()
            before = self._executor.set(device_id, target)
            latency = (time.monotonic() - t0) * 1000
            if before is None:
                log.warning("音量执行失败 device=%s", device_id)
                return
            action_id = self._m.log_action(
                self.name, None, pref["id"],
                {"type": "set_volume", "device_id": device_id,
                 "before": before, "after": target},
                effective_mode, action_ts,
                undo_payload={"restore": before}, latency_ms=latency)
            if effective_mode == "notify":
                self._notify("knowbot 已调音量",
                             f"按你的习惯调到 {target:.0%}（面板可撤销）",
                             self._calibrate_actions(pref["id"], action_id))
        self._m.add_hit(pref["id"])

        agent = AgentAction(exec_mode=effective_mode, from_loudness=before,
                            target_loudness=target)
        self._open_watch(device_id, inst_key, class_key, action_id, pref["id"],
                         action_ts, agent=agent, window_s=self._p.watch_window_s,
                         trigger=trigger)
        log.info("音量技能出手：device=%s mode=%s target=%s", device_id[-12:],
                 effective_mode, target)

    # ---------- 观察 ----------

    def _on_volume(self, ev: Event) -> None:
        if ev.type != "changed":
            return
        p = ev.payload
        if p.get("actor") != "user" or p.get("knob") != "master":
            return  # 只关心用户对 master 的调整（agent 自己的写不构成纠正）
        w = self._watches.get(p.get("device_id"))
        if w is not None and w.adjustment is None:  # 只取窗口内第一次静止调整
            w.adjustment = UserAdjustment(
                delta_s=max(0.0, ev.ts_mono - w.action_ts_mono),
                knob="master",
                from_loudness=p.get("from"),
                to_loudness=p.get("scalar", 0.0),
                burst_count=p.get("burst", 1),
            )

    def _open_watch(self, device_id, inst_key, class_key, action_id, pref_id,
                    action_ts, agent, window_s, trigger=None) -> None:
        sampler = None
        if self._meter_factory is not None:
            try:
                sampler = self._meter_factory(device_id)
                sampler.start()
            except Exception:
                log.debug("电平采样启动失败", exc_info=True)
        self._watches[device_id] = _Watch(
            device_id=device_id, inst_key=inst_key, class_key=class_key,
            action_id=action_id, pref_id=pref_id, action_ts_mono=action_ts,
            agent=agent, expires_at=time.monotonic() + window_s, sampler=sampler,
            trigger=trigger)

    def _finalize(self, w: "_Watch") -> None:
        meter = None
        if w.sampler is not None:
            stats = w.sampler.stop()
            meter = MeterStats(**stats) if stats else None
        case = VolumeCase(agent=w.agent, trigger_matched=True,
                          adjustment=w.adjustment, meter=meter)
        verdict = attribute(case, self._p)
        # 触发事件此刻已过观察窗（≥ flush 周期），反查补上台账/证据的溯源链
        trigger_event_id = (self._m.find_event_id(*w.trigger)
                            if w.trigger is not None else None)
        detail = {
            "delta_s": round(w.adjustment.delta_s, 2) if w.adjustment else None,
            "anchor": verdict.anchor_loudness,
            "meter": meter and {"mean": round(meter.mean, 3),
                                "peak": round(meter.peak, 3),
                                "active_frac": round(meter.active_frac, 3)},
        }
        result = self._learner.apply(verdict, action_id=w.action_id,
                                     pref_id=w.pref_id, device_key=w.inst_key,
                                     trigger_event_id=trigger_event_id, detail=detail)
        log.info("归因完成：rule=%s reaction=%s quadrant=%s trigger_event_id=%s",
                 verdict.rule, verdict.reaction, verdict.quadrant, trigger_event_id)
        if result.get("draft_created"):
            self._notify("knowbot 学到你的偏好",
                         f"这个设备的音量 {result['draft_value']:.0%}——对吗？",
                         self._calibrate_actions(result["draft_pref_id"], w.action_id))

    def _calibrate_actions(self, pref_id: int, action_id: int) -> list:
        """三键校准按钮（toast protocol 激活回传）。"""
        return [("太轻", calibrate_uri("soft", pref_id, action_id)),
                ("刚好", calibrate_uri("ok", pref_id, action_id)),
                ("太吵", calibrate_uri("loud", pref_id, action_id))]

    def _shadow_notify(self, device_id: str, title: str, body: str,
                       actions: list | None = None) -> None:
        """影子决策通知的节流闸：同设备冷却 + 全局日预算（超限静默落台账）。"""
        import datetime as _dt
        now = time.monotonic()
        today = _dt.date.today().isoformat()
        if self._toast_day[0] != today:
            self._toast_day = (today, 0)
        if self._toast_day[1] >= self._toast_daily:
            log.info("影子 toast 日预算已尽（%d），静默落台账", self._toast_daily)
            return
        last = self._toast_ts.get(device_id)
        if last is not None and now - last < self._toast_cooldown_s:
            log.info("设备 %s 影子 toast 冷却中，静默落台账", device_id[-12:])
            return
        self._toast_ts[device_id] = now
        self._toast_day = (today, self._toast_day[1] + 1)
        self._notify(title, body, actions)

    def _notify(self, title: str, body: str, actions: list | None = None) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier(title, body, actions)
        except TypeError:
            self._notifier(title, body)  # 旧签名 notifier（无按钮）——不因升级拖垮通知
        except Exception:
            log.debug("通知发送失败", exc_info=True)


class _Watch:
    """单个设备的观察窗口（执行后 A 窗 / 无信念 C 窗）。
    trigger=(sensor, type, device_id, ts_mono)：finalize 时反查 event_raw.id。"""

    def __init__(self, device_id, inst_key, class_key, action_id, pref_id,
                 action_ts_mono, agent, expires_at, sampler=None, trigger=None):
        self.device_id = device_id
        self.inst_key = inst_key
        self.class_key = class_key
        self.action_id = action_id
        self.pref_id = pref_id
        self.action_ts_mono = action_ts_mono
        self.agent = agent
        self.expires_at = expires_at
        self.sampler = sampler
        self.trigger = trigger
        self.adjustment: UserAdjustment | None = None
