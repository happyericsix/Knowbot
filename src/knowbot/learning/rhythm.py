"""节律观察层（Phase 3 入场券 §5 + 修订 §19 反驳修订版）。

分工：RhythmObserver 纯采集（窗口收集 + evidence 落库 + 空壳信念容器）；
ComboStats 统计是 **v1 正式机制**（节律归纳属"查表问题"领地，哲学 1），
研究层 agent 的领地是泛化与开放世界推断，不是基础归纳。两个已登记
缺口（壳进程污染 / 置信公式校准）见 §19.3，待真实数据校准。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from datetime import datetime

from ..bus import EventBus
from ..store.memory import MemoryStore

log = logging.getLogger(__name__)

PREF_KEY = "startup.launch_set"


def schedule_bucket(ts_wall: str) -> str:
    """工作日/周末分桶（纯函数：周一~五 weekday，六日 weekend）。"""
    try:
        dow = datetime.fromisoformat(ts_wall).weekday()  # 0=周一
    except (ValueError, TypeError):
        return "schedule:weekday"
    return "schedule:weekend" if dow >= 5 else "schedule:weekday"


class RhythmObserver:
    """纯采集：解锁 → 观察窗 → 窗口 exe 集合 → evidence 落库。

    零通知零动作；分析（若注入 analyzer）在窗口收尾后调用——
    分析器可插拔，占位实现见 StatsDraftAnalyzer。
    """

    name = "startup.observer"

    def __init__(self, bus: EventBus, memory: MemoryStore,
                 window_s: float = 900.0, analyzer=None,
                 poll_s: float = 1.0) -> None:
        self._bus = bus
        self._m = memory
        self._window_s = window_s
        self._analyzer = analyzer
        self._poll_s = poll_s
        self._win: dict | None = None
        self._task: asyncio.Task | None = None

    def start(self, loop) -> None:
        self._bus.subscribe("system.session", self._on_session)
        self._bus.subscribe("app.foreground", self._on_fg)
        self._task = loop.create_task(self._run(), name="rhythm-observer")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # ---- 事件入口 ----

    def _on_session(self, ev) -> None:
        if ev.type != "session_unlock":
            return
        # 新解锁覆盖旧窗口（短间隙再解锁 = 窗口重启）
        self._win = {"start": ev.ts_mono, "wall": ev.ts_wall, "exes": set()}

    def _on_fg(self, ev) -> None:
        if ev.type != "changed" or self._win is None:
            return
        exe = ev.payload.get("exe")
        if exe:  # 只记 exe 名（纯标识，红线）；快照缺失的跳过
            self._win["exes"].add(exe)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_s)
            w = self._win
            if w is not None and time.monotonic() - w["start"] >= self._window_s:
                self._win = None
                try:
                    self._finalize(w)
                except Exception:
                    log.exception("节律窗口收尾失败")

    # ---- 收尾：证据落库（采集终点）；分析交给注入的 analyzer ----

    def _finalize(self, w: dict) -> None:
        bucket = schedule_bucket(w["wall"])
        # 解锁事件此刻已落库（观察窗 >> flush 周期）；无设备标识按时间反查
        unlock_id = self._m.find_event_id(
            "system.session", "session_unlock", None, w["start"],
            tol_s=self._window_s + 120.0)
        # 空壳信念 = 节律证据的容器（value 恒空，成员由分析器/研究层填写）
        pref = self._m.get_or_create(
            PREF_KEY, "class", bucket, {"apps": []}, 0.25,
            "observed", state="draft")
        self._m.insert_evidence(
            pref["id"], "event", unlock_id,
            {"bucket": bucket, "window_exes": sorted(w["exes"]),
             "hhmm": w["wall"][11:16]})
        log.info("节律[%s] 窗口采集完成：%d 个程序（证据 #%s）",
                 bucket, len(w["exes"]), unlock_id)
        if self._analyzer is not None:
            try:
                self._analyzer.analyze(pref["id"], bucket)
            except Exception:
                log.exception("节律分析器执行失败（占位策略不应影响采集）")


class StatsDraftAnalyzer:
    """定量统计生成组合草稿——v1 正式机制（修订 §19 反驳修订版）。

    职责定位：生产"够格被用户纠正的草稿"（个人纠正是唯一 ground truth），
    不追求完美归纳。已登记缺口（§19.3，待数据校准）：
    - 壳进程污染：解锁后的系统前台抖动会混入窗口集合；
    - 置信公式 0.30+0.015n 系先验设定，≥10 窗口后按纠正史校准。
    研究层 agent 的领地是泛化/开放世界推断，不是基础归纳。
    """

    def __init__(self, memory: MemoryStore, min_samples: int = 5,
                 rate_floor: float = 0.4, max_members: int = 8) -> None:
        self._m = memory
        self._min_samples = min_samples
        self._rate_floor = rate_floor
        self._max_members = max_members

    def analyze(self, pref_id: int, bucket: str) -> None:
        pref = self._m.get_pref(pref_id)
        if pref is None:
            return
        windows = self._windows_from_evidence(pref_id, bucket)
        if len(windows) < self._min_samples:
            return
        counts: Counter = Counter()
        for exes in windows:
            counts.update(exes)
        n = len(windows)
        apps = [{"exe": e, "rate": round(c / n, 2)}
                for e, c in counts.most_common()
                if c / n >= self._rate_floor][:self._max_members]
        value = dict(json.loads(pref["value"]))
        value["apps"] = apps
        # 置信随样本缓升但封顶 0.45：draft 永远到不了建议档（用户确认才升）
        conf = min(0.45, 0.30 + 0.015 * n)
        self._m.update_preference(pref_id, value=value, confidence=conf)
        log.info("节律[%s] 占位统计更新：%d 窗口 %d 成员 %s", bucket, n,
                 len(apps), [a["exe"] for a in apps[:4]])

    def _windows_from_evidence(self, pref_id: int, bucket: str) -> list[set]:
        rows = self._m.conn.execute(
            "SELECT detail FROM evidence WHERE pref_id=? AND kind='event'"
            " ORDER BY id DESC LIMIT 30", (pref_id,)).fetchall()
        out = []
        for r in rows:
            try:
                d = json.loads(r["detail"] or "{}")
            except (ValueError, TypeError):
                continue
            if d.get("bucket") == bucket and d.get("window_exes") is not None:
                out.append(set(d["window_exes"]))
        return out
