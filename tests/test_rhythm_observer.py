"""节律观察层测试：窗口收集 / 分桶 / 最少样本 / 成员淘汰（入场券 §5）。"""
import asyncio
import json

from knowbot.bus import EventBus
from knowbot.envelope import Event
from knowbot.learning.rhythm import PREF_KEY, RhythmObserver, schedule_bucket
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore


def _setup(tmp_path, **kw):
    store = EventStore(tmp_path / "r.db", batch_size=10_000, flush_interval=0.05)
    memory = MemoryStore(store._conn)
    return store, memory


def _unlock(wall="2026-10-29T09:00:00+08:00"):
    return Event("system.session", "session_unlock", {}, ts_wall=wall)


def _fg(exe):
    return Event("app.foreground", "changed", {"exe": exe, "pid": 1})


def test_schedule_bucket():
    assert schedule_bucket("2026-10-26T07:30:00+08:00") == "schedule:weekday"  # 周一
    assert schedule_bucket("2026-10-31T10:00:00+08:00") == "schedule:weekend"  # 周六
    assert schedule_bucket("2026-11-01T10:00:00+08:00") == "schedule:weekend"  # 周日
    assert schedule_bucket("not-a-date") == "schedule:weekday"          # 容错


def test_window_writes_evidence_with_trace(tmp_path):
    """解锁 → 窗口收集 → 证据落库（ref 指向解锁事件，可溯源可重放）。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        bus.subscribe("", store.append)
        flusher = asyncio.create_task(store.run())
        runner = asyncio.create_task(bus.run())
        obs = RhythmObserver(bus, memory, window_s=0.15, poll_s=0.02,
                             min_samples=5)
        obs.start(asyncio.get_running_loop())

        bus.publish(_unlock())
        for exe in ("ZCode.exe", "msedge.exe", "ZCode.exe"):  # 重复也只记集合
            bus.publish(_fg(exe))
        await asyncio.sleep(0.4)
        obs.stop(); runner.cancel(); flusher.cancel()
        await asyncio.gather(runner, flusher, return_exceptions=True)

        bucket = schedule_bucket("2026-10-29T09:00:00+08:00")
        pref = memory.get(PREF_KEY, "class", bucket)
        assert pref is not None and pref["state"] == "draft"
        ev = memory.conn.execute(
            "SELECT ref_id, detail FROM evidence WHERE pref_id=? AND kind='event'",
            (pref["id"],)).fetchone()
        d = json.loads(ev["detail"])
        assert d["window_exes"] == ["ZCode.exe", "msedge.exe"]  # 排序集合
        assert d["bucket"] == bucket and d["hhmm"] == "09:00"
        unlock = memory.conn.execute(
            "SELECT id FROM event_raw WHERE type='session_unlock'").fetchone()
        assert ev["ref_id"] == unlock["id"]                     # 溯源链闭合
        assert json.loads(pref["value"])["apps"] == []          # 样本不足：成员空
        assert float(pref["confidence"]) < 0.5                  # draft 到不了建议档
        store.close()

    asyncio.run(main())


def test_combo_forms_after_min_samples_with_elimination(tmp_path):
    """5 窗口后组合成形：常客进成员，低频者被淘汰线剔除（衰减淘汰）。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        bus.subscribe("", store.append)
        flusher = asyncio.create_task(store.run())
        runner = asyncio.create_task(bus.run())
        obs = RhythmObserver(bus, memory, window_s=0.15, poll_s=0.02,
                             min_samples=4, rate_floor=0.4)
        obs.start(asyncio.get_running_loop())

        plan = [("A.exe",), ("A.exe", "B.exe"), ("A.exe", "B.exe"),
                ("A.exe", "C.exe")]  # A 4/4、B 2/4、C 1/4
        for exes in plan:
            bus.publish(_unlock())
            for exe in exes:
                bus.publish(_fg(exe))
            await asyncio.sleep(0.35)
        obs.stop(); runner.cancel(); flusher.cancel()
        await asyncio.gather(runner, flusher, return_exceptions=True)

        bucket = schedule_bucket("2026-10-29T09:00:00+08:00")
        pref = memory.get(PREF_KEY, "class", bucket)
        apps = {a["exe"]: a["rate"] for a in json.loads(pref["value"])["apps"]}
        assert apps == {"A.exe": 1.0, "B.exe": 0.5}   # C 0.25 < 0.4 淘汰
        assert float(pref["confidence"]) == 0.36      # min(0.45, 0.30+0.015*4)
        assert pref["state"] == "draft"
        store.close()

    asyncio.run(main())
