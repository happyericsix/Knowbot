"""启动技能（Phase 3 入场券实施）：解锁后开工组合助手。

档位与规则编号严格对应 docs/phase3-startup-skill.md §5/§6：
- 组合草稿（conf<0.5）→ 静默观察（SN2），观察层照常记录；
- 用户面板确认（≥0.5）→ 建议档：解锁 +suggest_delay 后，若手动开组合
  成员 < silence_ratio 则弹建议 toast（S1 拉起/S2 不用）；≥ 则彻底沉默
  并记 missed_need（S4——升档燃料）；
- value.auto=true 且 conf≥0.85 → 执行档：静默拉起缺失成员（防重复），
  拉起后被全关且从未前台 → S6 自动撤守（confidence ×γ_hard + 关 auto）。
深夜（0-6 点）解锁不触发（SN1）；全局暂停随时生效；台账不可豁免。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from ..bus import EventBus
from ..learning.rhythm import PREF_KEY, schedule_bucket
from ..notify.protocol import startup_uri
from ..store.memory import MemoryStore

log = logging.getLogger(__name__)

CONF_SUGGEST = 0.50
CONF_EXEC = 0.85


@dataclass(frozen=True)
class StartupParams:
    suggest_delay_s: float = 90.0     # 等你自己开首波程序再开口
    silence_ratio: float = 0.60       # 手动开 ≥ 此比例 → 沉默 + missed_need
    night_start: int = 0              # 深夜时段（含）——不触发
    night_end: int = 6                # 深夜时段（不含）
    min_missed_for_auto: int = 3      # missed_need 累计值达标才提供"自动拉起"
    exec_watch_s: float = 300.0       # 执行档 S6 观察窗（拉起后 5 分钟）


class StartupSkill:
    name = "startup"

    def __init__(self, bus: EventBus, memory: MemoryStore, launcher,
                 notifier=None, params: StartupParams | None = None) -> None:
        self._bus = bus
        self._m = memory
        self._launcher = launcher
        self._notifier = notifier
        self._p = params or StartupParams()
        self._seen: set[str] = set()      # 本次解锁后手动开过的组合成员
        self._armed = False               # 解锁已挂起待建议/执行
        self._loop = None

    def start(self, loop) -> None:
        self._loop = loop
        self._bus.subscribe("system.session", self._on_session)
        self._bus.subscribe("app.foreground", self._on_fg)

    def stop(self) -> None:
        pass  # call_later 回调随事件循环生命周期结束

    # ---- 触发 ----

    def _on_session(self, ev) -> None:
        if ev.type != "session_unlock" or self._armed:
            return
        if self._m.get_meta("paused") == "1":
            log.info("全局暂停中，启动技能不出手")
            return
        try:
            hour = datetime.fromisoformat(ev.ts_wall).hour
        except ValueError:
            hour = 12
        if self._p.night_start <= hour < self._p.night_end:
            return  # SN1：深夜解锁不是开工节律
        rows = self._m.resolve(PREF_KEY, "", schedule_bucket(ev.ts_wall))
        pref = rows[0] if rows else None
        if pref is None or float(pref["confidence"]) < CONF_SUGGEST:
            return  # SN2：无确认组合（观察层 RhythmObserver 照常记录）
        conf = float(pref["confidence"])
        value = json.loads(pref["value"])
        if not value.get("apps"):
            return
        self._seen = set()
        self._armed = True
        fire = self._exec if (conf >= CONF_EXEC and value.get("auto")) else self._suggest
        self._loop.call_later(self._p.suggest_delay_s, fire, ev.ts_mono, pref["id"])

    def _on_fg(self, ev) -> None:
        if ev.type == "changed" and self._armed:
            exe = ev.payload.get("exe")
            if exe:
                self._seen.add(exe)

    # ---- 建议档（S1-S4）----

    def _suggest(self, ts_mono: float, pref_id: int) -> None:
        self._armed = False
        if self._m.get_meta("paused") == "1":
            return
        pref = self._m.get_pref(pref_id)
        if pref is None or pref["state"] != "active":
            return
        members = [a["exe"] for a in json.loads(pref["value"]).get("apps", [])]
        if not members:
            return
        ratio = (len(self._seen.intersection(members)) / len(members)
                 if members else 0.0)
        if ratio >= self._p.silence_ratio:
            # S4：你没等它——missed_need 是升档燃料，比弹窗珍贵（计算礼貌）
            action_id = self._m.log_action(
                self.name, None, pref_id,
                {"type": "suggest_suppressed", "manual_ratio": round(ratio, 2),
                 "manual_exes": sorted(self._seen.intersection(members))},
                "none", ts_mono)
            self._m.finish_action(action_id, "missed_need", "missed_need",
                                  {"rule": "S4"})
            _bump_value_counter(self._m, pref_id, "missed_count")
            log.info("启动技能 S4：手动比例 %.0f%%，沉默记 missed_need", ratio * 100)
            return
        action_id = self._m.log_action(
            self.name, None, pref_id,
            {"type": "suggest", "combo": members}, "suggest", ts_mono)
        buttons = [("拉起组合", startup_uri("launch", pref_id, action_id)),
                   ("不用", startup_uri("dismiss", pref_id, action_id))]
        value = json.loads(pref["value"])
        if value.get("missed_count", 0) >= self._p.min_missed_for_auto:
            buttons.insert(0, ("以后自动拉起", startup_uri("auto", pref_id, action_id)))
        self._notify("knowbot 开工组合",
                     f"要拉起 {', '.join(members[:3])}"
                     f"{'…' if len(members) > 3 else ''} 吗？", buttons)

    # ---- 执行档（S6 撤守）----

    def _exec(self, ts_mono: float, pref_id: int) -> None:
        self._armed = False
        if self._m.get_meta("paused") == "1":
            return
        pref = self._m.get_pref(pref_id)
        if pref is None:
            return
        members = [a["exe"] for a in json.loads(pref["value"]).get("apps", [])]
        missing = [e for e in members if e not in self._seen]
        if not missing:
            return  # 你已全部手动开好：本次无需出手（不落账，非决策）
        result = self._launcher.launch(missing)
        action_id = self._m.log_action(
            self.name, None, pref_id,
            {"type": "launch", "combo": members, **result}, "silent", ts_mono,
            undo_payload={"launched": result.get("launched", [])})
        if result.get("launched"):
            self._loop.call_later(self._p.exec_watch_s, self._exec_watch,
                                  action_id, pref_id,
                                  {i["exe"].lower() for i in result["launched"]})
        log.info("启动技能执行档：拉起 %s 跳过 %s",
                 [i["exe"] for i in result.get("launched", [])],
                 result.get("skipped_running", []) + result.get("unresolved", []))

    def _exec_watch(self, action_id: int, pref_id: int, launched: set[str]) -> None:
        """S6：拉起后 5 分钟内全部退出且从未前台 → 拉错了，撤守。"""
        try:
            from ..executors.launch import running_exes
            still = running_exes()
            alive = launched.intersection(still)
            if alive or (self._seen and launched.intersection(
                    {e.lower() for e in self._seen})):
                return  # 还活着或曾到前台：视为有效
            pref = self._m.get_pref(pref_id)
            self._m.ratchet_correct(pref_id, 0.5)
            if pref is not None:
                value = dict(json.loads(pref["value"]))
                value["auto"] = False  # 撤守：关自动拉起，回建议档
                self._m.update_preference(pref_id, value=value)
            self._m.finish_action(action_id, "correct", "false_alarm",
                                  {"rule": "S6"})
            log.info("启动技能 S6：拉起即被弃，撤守自动档")
        except Exception:
            log.exception("S6 撤守检查失败")

    def _notify(self, title: str, body: str, actions: list | None = None) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier(title, body, actions)
        except TypeError:
            self._notifier(title, body)
        except Exception:
            log.debug("通知发送失败", exc_info=True)


def _bump_value_counter(memory: MemoryStore, pref_id: int, key: str) -> None:
    pref = memory.get_pref(pref_id)
    if pref is None:
        return
    value = dict(json.loads(pref["value"]))
    value[key] = value.get(key, 0) + 1
    memory.update_preference(pref_id, value=value)


# ---- toast 按钮回传（CLI knowbot action 与面板共用）----

def apply_startup_action(memory: MemoryStore, launcher, op: str,
                         pref_id: int, action_id: int | None,
                         via: str = "toast") -> dict:
    """S1/S2/auto 三键的落库与执行（与技能本体同一套规则编号）。"""
    if op not in ("launch", "dismiss", "auto"):
        return {"ok": False, "error": f"非法操作: {op}"}
    pref = memory.get_pref(pref_id)
    if pref is None:
        return {"ok": False, "error": f"信念不存在: {pref_id}"}
    action = memory.get_action(action_id) if action_id is not None else None
    if action is not None and action["reaction"] is not None:
        return {"ok": False, "error": "该动作已有裁决，拒绝重复操作"}

    members = [a["exe"] for a in json.loads(pref["value"]).get("apps", [])]
    if op == "launch":
        result = launcher.launch(members)
        if action_id is not None:
            memory.finish_action(action_id, "accept", "correct_detection",
                                 {"rule": "S1", **result})
        return {"ok": True, "op": op, **result}

    if op == "dismiss":
        memory.ratchet_correct(pref_id, 0.8)
        value = dict(json.loads(pref["value"]))
        value["dismiss_count"] = value.get("dismiss_count", 0) + 1
        if value["dismiss_count"] >= 2:
            value["auto"] = False
            memory.update_preference(pref_id, value=value, confidence=0.30)
        else:
            memory.update_preference(pref_id, value=value)
        if action_id is not None:
            memory.finish_action(action_id, "correct", "false_alarm",
                                 {"rule": "S2", "via": via})
        return {"ok": True, "op": op, "confidence_after":
                memory.get_pref(pref_id)["confidence"]}

    # op == "auto"：missed_need 达标才允许升执行档（入场券 §8）
    value = dict(json.loads(pref["value"]))
    if value.get("missed_count", 0) < 3:
        return {"ok": False, "error": "missed_need 未达标，拒绝开通自动拉起"}
    value["auto"] = True
    memory.update_preference(pref_id, value=value, confidence=0.90,
                             source="explicit", state="active")
    if action_id is not None:
        memory.finish_action(action_id, "accept", "correct_detection",
                             {"rule": "S1-auto", "via": via})
    return {"ok": True, "op": op, "auto": True}
