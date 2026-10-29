"""泛化提案管道（Phase 4 入场券：L1 实例→类，确定性机械）。

定位（修订 §19 反驳修订版）：确定性泛化属查表领地；agent 研究层的领地
是跨域/开放世界推断。提案只进待确认区，用户裁决才生效（哲学 3）。
设备的类归属（form_factor）从 event_raw 事实流反查——信念只存偏好，
事实留在录像带里。
"""
from __future__ import annotations

import asyncio
import json
import logging

from ..store.memory import MemoryStore

log = logging.getLogger(__name__)

PREF_KEY = "audio.volume"
L1_MIN_INSTANCES = 2      # ≥2 条 explicit 实例才配泛化
L1_MIN_CONF = 0.7         # 逐条置信下限
L1_VALUE_TOL = 0.05       # 值一致容差（scalar 极差）
L1_TRANSFER_DISCOUNT = 0.85  # 迁移折扣：封顶不高于来源（A1 原文）


def _form_factor_of_device(memory: MemoryStore, device_id: str) -> str | None:
    """从事实流反查设备类归属：最近一条携带该 device_id 的 audio.flow 事件。"""
    rows = memory.conn.execute(
        "SELECT payload FROM event_raw WHERE sensor='audio.flow'"
        " AND payload LIKE ? ORDER BY id DESC LIMIT 5",
        (f'%"device_id": "{device_id}"%',)).fetchall()
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except (ValueError, TypeError):
            continue
        if p.get("device_id") == device_id and p.get("form_factor"):
            return p["form_factor"]
    return None


def find_l1_candidates(memory: MemoryStore) -> list[dict]:
    """扫描实例信念 → 按类分组的 L1 候选（纯读，不写库）。"""
    rows = memory.conn.execute(
        "SELECT * FROM preference WHERE pref_key=? AND scope='instance'"
        " AND state='active' AND source='explicit'", (PREF_KEY,)).fetchall()
    by_class: dict[str, list] = {}
    for r in rows:
        device_id = r["scope_key"].split("audio.device:", 1)[-1]
        ff = _form_factor_of_device(memory, device_id)
        if ff:
            by_class.setdefault(f"audio.form_factor:{ff}", []).append(r)
    out = []
    for class_key, insts in by_class.items():
        if len(insts) < L1_MIN_INSTANCES:
            continue
        if any(float(r["confidence"]) < L1_MIN_CONF for r in insts):
            continue
        scalars = [json.loads(r["value"]).get("scalar") for r in insts]
        if any(s is None for s in scalars):
            continue
        if max(scalars) - min(scalars) > L1_VALUE_TOL:
            continue  # 值分歧：泛化无共识，宁可不学
        cls = memory.get(PREF_KEY, "class", class_key)
        proposed = min(scalars)  # 保守侧
        out.append({
            "kind": "generalize_l1", "class_key": class_key,
            "value": {"scalar": proposed},
            "confidence": round(min(float(r["confidence"]) for r in insts)
                                * L1_TRANSFER_DISCOUNT, 4),
            "origin_pref_ids": [r["id"] for r in insts],
            "origin_confidence": min(float(r["confidence"]) for r in insts),
            "current": ({"scalar": json.loads(cls["value"]).get("scalar"),
                         "confidence": cls["confidence"],
                         "source": cls["source"]} if cls else None),
        })
    return out


def generate_proposals(memory: MemoryStore) -> int:
    """候选 → proposal 表（去重 + 拒绝抑制）。返回新建条数。"""
    n = 0
    for cand in find_l1_candidates(memory):
        dup = memory.conn.execute(
            "SELECT 1 FROM proposal WHERE kind=? AND status IN ('pending','accepted')"
            " AND content LIKE ?", (cand["kind"], f'%{cand["class_key"]}%')
        ).fetchone()
        if dup:
            continue  # pending 去重；已接受也去重（再提是打扰，纠正走用户）
        suppressed = memory.conn.execute(
            "SELECT 1 FROM exception WHERE pref_key=? AND scope_key=?",
            (PREF_KEY, cand["class_key"])).fetchone()
        if suppressed:
            continue  # 曾被拒绝：例外表一票否决
        origin = cand["origin_pref_ids"][0]
        memory.conn.execute(
            "INSERT INTO proposal (ts_wall, kind, content, confidence,"
            " origin_pref_id, status, notify_count) VALUES (?,?,?,?,?, 'pending', 0)",
            (_now(), cand["kind"],
             json.dumps({"pref_key": PREF_KEY, "scope": "class",
                         "scope_key": cand["class_key"],
                         "value": cand["value"], "from": cand["current"],
                         "instances": len(cand["origin_pref_ids"])},
                        ensure_ascii=False),
             cand["confidence"], origin))
        memory.conn.commit()
        n += 1
        log.info("泛化提案 #%s：L1 %s → %s (conf=%.3f)",
                 n, cand["class_key"], cand["value"], cand["confidence"])
    return n


def apply_proposal(memory: MemoryStore, proposal_id: int, accept: bool,
                   via: str = "panel") -> dict:
    """面板裁决入口：接受 → 类信念升格；拒绝 → exception 永久抑制。"""
    row = memory.conn.execute(
        "SELECT * FROM proposal WHERE id=?", (proposal_id,)).fetchone()
    if row is None or row["status"] != "pending":
        return {"ok": False, "error": "提案不存在或已裁决"}
    content = json.loads(row["content"])
    if accept:
        memory.update_preference(
            _class_pref_id(memory, content), value=content["value"],
            confidence=row["confidence"], source="inferred", state="active")
        memory.conn.execute(
            "UPDATE proposal SET status='accepted', decided_at=?, decided_via=?"
            " WHERE id=?", (_now(), via, proposal_id))
        memory.conn.commit()
        return {"ok": True, "status": "accepted",
                "scope_key": content["scope_key"]}
    memory.conn.execute(
        "INSERT INTO exception (pref_key, scope_key, reason, created_at)"
        " VALUES (?,?,?,?) ON CONFLICT(pref_key, scope_key) DO NOTHING",
        (content["pref_key"], content["scope_key"], "proposal_rejected", _now()))
    memory.conn.execute(
        "UPDATE proposal SET status='rejected', decided_at=?, decided_via=?"
        " WHERE id=?", (_now(), via, proposal_id))
    memory.conn.commit()
    return {"ok": True, "status": "rejected", "suppressed": True}


def _class_pref_id(memory: MemoryStore, content: dict) -> int:
    pref = memory.get_or_create(
        content["pref_key"], content["scope"], content["scope_key"],
        content["value"], 0.5, "community", state="active")
    return pref["id"]


def _now() -> str:
    from ..store.memory import now_iso
    return now_iso()


class Generalizer:
    """daemon 内周期任务：扫描 → 生成提案（1h 一拍，纯 SQL，计算礼貌）。"""

    name = "generalizer"

    def __init__(self, memory: MemoryStore, interval_s: float = 3600.0) -> None:
        self._m = memory
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None

    def start(self, loop) -> None:
        self._task = loop.create_task(self._run(), name="generalizer")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                generate_proposals(self._m)
            except Exception:
                log.exception("泛化扫描失败")
            await asyncio.sleep(self._interval_s)
