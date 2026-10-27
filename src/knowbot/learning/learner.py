"""音量学习器：Verdict → preference/evidence/action_log 更新；三键校准入口。"""
from __future__ import annotations

import json
import logging

from ..attribution.volume import Verdict
from ..store.memory import MemoryStore

log = logging.getLogger(__name__)

DAILY_CORRECTION_CAP = 3  # 单日冻结阈值（A2 §5 护栏 4）
CALIBRATION_GAMMA = 0.8   # 太轻/太吵：方向明确但幅度未知 → 轻罚，值不动
CALIBRATION_CONF = 0.9    # 刚好：显式确认（memory-ddl §7：explicit / 0.9 / active）


class VolumeLearner:
    def __init__(self, memory: MemoryStore) -> None:
        self._m = memory

    def apply(self, verdict: Verdict, *, action_id: int | None,
              pref_id: int | None, device_key: str | None,
              trigger_event_id: int | None, detail: dict) -> dict:
        """应用一次归因裁决。所有分支都回填台账。返回执行摘要（供通知层）。"""
        result: dict = {"reaction": verdict.reaction, "rule": verdict.rule}
        try:
            if verdict.reaction == "accept" and pref_id is not None:
                self._m.ratchet_confirm(pref_id, verdict.lambda_up or 0.15)
                self._m.insert_evidence(pref_id, "action", action_id,
                                        {"rule": verdict.rule, **detail})

            elif verdict.reaction == "correct" and pref_id is not None:
                # 单日纠正上限：防异常日毁掉长期学习（护栏 4）
                if self._m.corrections_today(pref_id) < DAILY_CORRECTION_CAP:
                    self._m.ratchet_correct(pref_id, verdict.gamma or 0.8)
                else:
                    log.info("偏好 %s 当日纠正已达上限，冻结置信度调整", pref_id)
                self._m.insert_evidence(pref_id, "correction", action_id,
                                        {"rule": verdict.rule,
                                         "gamma": verdict.gamma, **detail})
                if verdict.anchor_loudness is not None and device_key:
                    self._m.append_anchor(device_key, verdict.anchor_loudness)

            elif verdict.reaction == "missed_need":
                # 本该我出手：静止值直接是学习原料 → 实例级草稿信念
                if verdict.anchor_loudness is not None and device_key:
                    row = self._m.get_or_create(
                        "audio.volume", "instance", device_key,
                        {"scalar": verdict.anchor_loudness}, 0.35, "observed",
                        state="draft")
                    self._m.insert_evidence(row["id"], "event", trigger_event_id,
                                            {"rule": verdict.rule, **detail})
                    result["draft_created"] = True
                    result["draft_value"] = verdict.anchor_loudness
                    result["draft_pref_id"] = row["id"]

            self._m.finish_action(action_id, verdict.reaction, verdict.quadrant,
                                  {"rule": verdict.rule, **detail},
                                  trigger_event_id=trigger_event_id)
        except Exception:
            log.exception("学习器应用裁决失败 rule=%s", verdict.rule)
        return result

    # ---- 三键校准（太轻/刚好/太吵）----

    def apply_calibration(self, value: str, pref_id: int,
                          action_id: int | None = None, via: str = "panel") -> dict:
        """三键校准一次落库：信念 + 证据 + 台账回填。

        语义（docs/design-amendments.md §12，依据 memory-ddl §7）：
        - 刚好 = 显式确认：实例级信念诞生/升格（explicit / ≥0.9 / active），
          锚点照记，台账回填 accept/correct_detection；
        - 太轻/太吵 = 方向性纠正：置信度 ×γ(0.8)，**值不动**——只有方向没有
          幅度，猜幅度就是学错（哲学 5）；真实幅度留给 A 规则的锚点机制。
        台账已回填过的动作拒绝重复校准（双击/两处入口防重）。
        """
        if value not in ("soft", "ok", "loud"):
            return {"ok": False, "error": f"非法校准值: {value}"}
        pref = self._m.get_pref(pref_id)
        if pref is None:
            return {"ok": False, "error": f"信念不存在: {pref_id}"}
        action = self._m.get_action(action_id) if action_id is not None else None
        if action_id is not None and action is None:
            return {"ok": False, "error": f"台账不存在: {action_id}"}
        if action is not None and action["reaction"] is not None:
            return {"ok": False, "error": "该动作已有裁决，拒绝重复校准"}

        # 校准对象值：动作的目标值优先（校准的是"这次出手"），否则信念自身值
        target = None
        if action is not None:
            a = json.loads(action["action"])
            target = a.get("after", a.get("target"))
        if target is None:
            target = json.loads(pref["value"]).get("scalar")

        # 设备作用域：动作携带 device_id（校准出手）或信念本身是实例级（面板校准）
        device_key = None
        if action is not None:
            d = json.loads(action["action"]).get("device_id")
            if d:
                device_key = f"audio.device:{d}"
        elif pref["scope"] == "instance":
            device_key = pref["scope_key"]

        if value == "ok":
            final_id, created = pref_id, False
            if device_key and pref["scope"] != "instance":
                row = self._m.get_or_create(
                    "audio.volume", "instance", device_key,
                    {"scalar": target}, CALIBRATION_CONF, "explicit", state="active")
                final_id, created = row["id"], True
                pref = row
            conf = max(float(pref["confidence"]), CALIBRATION_CONF)
            self._m.update_preference(final_id, confidence=conf,
                                      source="explicit", state="active")
            if device_key and target is not None:
                self._m.append_anchor(device_key, target)
            self._m.insert_evidence(final_id, "calibration", action_id,
                                    {"value": "ok", "target": target, "via": via,
                                     "rule": "CAL-OK"})
            if action is not None:
                self._m.finish_action(action_id, "accept", "correct_detection",
                                      {"rule": "CAL-OK", "via": via})
            return {"ok": True, "value": value, "pref_id": final_id,
                    "created_instance": created, "confidence": conf,
                    "action_backfilled": action is not None}

        # 太轻 / 太吵：轻罚不动值
        rule = f"CAL-{value.upper()}"
        self._m.ratchet_correct(pref_id, CALIBRATION_GAMMA)
        self._m.insert_evidence(pref_id, "calibration", action_id,
                                {"value": value, "target": target, "via": via,
                                 "rule": rule})
        if action is not None:
            self._m.finish_action(action_id, "correct", "false_alarm",
                                  {"rule": rule, "via": via})
        return {"ok": True, "value": value, "pref_id": pref_id,
                "created_instance": False,
                "action_backfilled": action is not None}
