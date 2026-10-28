"""节律观察层测试（修订 §19：纯采集 + 可插拔分析器）。

- 采集：窗口收集 / 分桶 / 证据溯源（value 恒空，不做任何分析）；
- 占位分析器：样本门槛 / 成员淘汰（只为跑通流程，长期待研究层替换）。
"""
import asyncio
import json

from knowbot.bus import EventBus
from knowbot.envelope import Event
from knowbot.learning.rhythm import (PREF_KEY, RhythmObserver,
                                     StatsDraftAnalyzer, schedule_bucket)
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore


def _setup(tmp_path):
    store = EventStore(tmp_path / "r.db", batch_size=10_000, flush_interval=0.05)
    memory = MemoryStore(store._conn)
    return store, memory


def _unlock(wall="2026-10-29T09:00:00+08:00"):
    return Event("system.session", "session_unlock", {}, ts_wall=wall)


def _fg(exe):
    return Event("app.foreground", "changed", {"exe": exe, "pid": 1})


async def _run_observer(bus, obs, script, settle=0.4):
    runner = asyncio.create_task(bus.run())
    for item in script:
        bus.publish(item)
        await asyncio.sleep(0.05)
    await asyncio.sleep(settle)
    obs.stop()
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)


def test_schedule_bucket():
    assert schedule_bucket("2026-10-26T07:30:00+08:00") == "schedule:weekday"  # 周一
    assert schedule_bucket("2026-10-31T10:00:00+08:00") == "schedule:weekend"  # 周六
    assert schedule_bucket("2026-11-01T10:00:00+08:00") == "schedule:weekend"  # 周日
    assert schedule_bucket("not-a-date") == "schedule:weekday"          # 容错


def test_collection_only_writes_evidence_without_analysis(tmp_path):
    """纯采集（无分析器）：窗口→证据（ref 指向解锁事件），信念壳 value 恒空。"""
    async def main():
        store, memory = _setup(tmp_path)
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        bus.subscribe("", store.append)
        flusher = asyncio.create_task(store.run())
        obs = RhythmObserver(bus, memory, window_s=0.15, poll_s=0.02,
                             analyzer=None)
        obs.start(loop)
        await _run_observer(bus, obs, [_unlock(),
                                       _fg("ZCode.exe"), _fg("msedge.exe"),
                                       _fg("ZCode.exe")])
        flusher.cancel()
        await asyncio.gather(flusher, return_exceptions=True)

        bucket = schedule_bucket("2026-10-29T09:00:00+08:00")
        pref = memory.get(PREF_KEY, "class", bucket)
        assert pref is not None and pref["state"] == "draft"
        assert json.loads(pref["value"]) == {"apps": []}   # 壳：不做分析
        assert float(pref["confidence"]) == 0.25
        ev = memory.conn.execute(
            "SELECT ref_id, detail FROM evidence WHERE pref_id=? AND kind='event'",
            (pref["id"],)).fetchone()
        d = json.loads(ev["detail"])
        assert d["window_exes"] == ["ZCode.exe", "msedge.exe"]  # 排序集合
        assert d["hhmm"] == "09:00"
        unlock = memory.conn.execute(
            "SELECT id FROM event_raw WHERE type='session_unlock'").fetchone()
        assert ev["ref_id"] == unlock["id"]                     # 溯源链闭合
        store.close()

    asyncio.run(main())


def test_placeholder_analyzer_forms_combo_with_elimination(tmp_path):
    """占位分析器（跑通流程用）：样本门槛 + 出现率淘汰 + 置信封顶。"""
    async def main():
        store, memory = _setup(tmp_path)
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        bus.subscribe("", store.append)
        flusher = asyncio.create_task(store.run())
        obs = RhythmObserver(
            bus, memory, window_s=0.15, poll_s=0.02,
            analyzer=StatsDraftAnalyzer(memory, min_samples=4, rate_floor=0.4))
        obs.start(loop)

        plan = [("A.exe",), ("A.exe", "B.exe"), ("A.exe", "B.exe"),
                ("A.exe", "C.exe")]  # A 4/4、B 2/4、C 1/4
        for exes in plan:
            await _run_observer(bus, obs, [_unlock()] + [_fg(e) for e in exes],
                                settle=0.25)
            obs.start(loop)  # _run_observer 里 stop 了，重启继续收
        flusher.cancel()
        await asyncio.gather(flusher, return_exceptions=True)

        bucket = schedule_bucket("2026-10-29T09:00:00+08:00")
        pref = memory.get(PREF_KEY, "class", bucket)
        apps = {a["exe"]: a["rate"] for a in json.loads(pref["value"])["apps"]}
        assert apps == {"A.exe": 1.0, "B.exe": 0.5}   # C 0.25 < 0.4 淘汰
        assert float(pref["confidence"]) == 0.36      # min(0.45, 0.30+0.015*4)
        assert pref["state"] == "draft"               # 永远到不了建议档
        store.close()

    asyncio.run(main())


def test_placeholder_below_min_samples_keeps_shell_empty(tmp_path):
    """样本不足：占位分析器不填成员（宁可不学不可学错）。"""
    async def main():
        store, memory = _setup(tmp_path)
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        obs = RhythmObserver(
            bus, memory, window_s=0.15, poll_s=0.02,
            analyzer=StatsDraftAnalyzer(memory, min_samples=5))
        obs.start(loop)
        await _run_observer(bus, obs, [_unlock(), _fg("A.exe")], settle=0.25)
        bucket = schedule_bucket("2026-10-29T09:00:00+08:00")
        pref = memory.get(PREF_KEY, "class", bucket)
        assert json.loads(pref["value"]) == {"apps": []}
        assert float(pref["confidence"]) == 0.25
        store.close()

    asyncio.run(main())
