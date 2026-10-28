"""启动技能黄金测试：S1-S7/SN 规则与 docs/phase3-startup-skill.md §6 对应。"""
import asyncio
import json
import time

from knowbot.bus import EventBus
from knowbot.envelope import Event
from knowbot.learning.rhythm import PREF_KEY
from knowbot.skills.startup import (StartupParams, StartupSkill,
                                    apply_startup_action)
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore

MORNING = "2026-10-29T09:00:00+08:00"   # 周四早晨
NIGHT = "2026-10-30T03:30:00+08:00"
BUCKET = "schedule:weekday"

P = StartupParams(suggest_delay_s=0.12, silence_ratio=0.6, exec_watch_s=0.12)


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def launch(self, exes):
        self.calls.append(list(exes))
        return {"launched": [{"exe": e, "pid": 100} for e in exes],
                "skipped_running": [], "unresolved": []}


class _Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, title, body, actions=None):
        self.calls.append((title, body, actions))


def _setup(tmp_path, conf=0.55, apps=("A.exe", "B.exe", "C.exe"), **value_kw):
    store = EventStore(tmp_path / "s.db", batch_size=10_000, flush_interval=0.05)
    memory = MemoryStore(store._conn)
    memory.get_or_create(PREF_KEY, "class", BUCKET,
                         {"apps": [{"exe": a, "rate": 1.0} for a in apps],
                          **value_kw},
                         conf, "explicit", state="active")
    pref = memory.get(PREF_KEY, "class", BUCKET)
    return store, memory, pref


def _unlock(wall=MORNING):
    return Event("system.session", "session_unlock", {}, ts_wall=wall)


def _fg(exe):
    return Event("app.foreground", "changed", {"exe": exe, "pid": 1})


async def _drive(bus, skill, script, settle=0.5):
    runner = asyncio.create_task(bus.run())
    t0 = time.monotonic()
    for delay, item in script:
        await asyncio.sleep(max(0.0, delay - (time.monotonic() - t0)))
        bus.publish(item() if callable(item) else item)
    await asyncio.sleep(settle)
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)


def test_sn1_night_unlock_skipped(tmp_path):
    """深夜（0-6 点）解锁不触发——不是开工节律。"""
    async def main():
        store, memory, pref = _setup(tmp_path)
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        rec = _Rec()
        skill = StartupSkill(bus, memory, FakeLauncher(), rec, P)
        skill.start(loop)
        await _drive(bus, skill, [(0.02, _unlock(NIGHT))], settle=0.4)
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 0 and rec.calls == []
        store.close()
    asyncio.run(main())


def test_sn2_unconfirmed_combo_silent(tmp_path):
    """组合未确认（conf<0.5）不出手——观察层照常记录，技能静默。"""
    async def main():
        store, memory, pref = _setup(tmp_path, conf=0.40)
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        rec = _Rec()
        skill = StartupSkill(bus, memory, FakeLauncher(), rec, P)
        skill.start(loop)
        await _drive(bus, skill, [(0.02, _unlock())], settle=0.4)
        assert rec.calls == []
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 0
        store.close()
    asyncio.run(main())


def test_suggest_flow_and_s1_launch(tmp_path):
    """建议档：手动比例低 → toast 建议；点[拉起组合] → accept + 执行器拉起。"""
    async def main():
        store, memory, pref = _setup(tmp_path, conf=0.55)
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        rec = _Rec()
        launcher = FakeLauncher()
        skill = StartupSkill(bus, memory, launcher, rec, P)
        skill.start(loop)
        await _drive(bus, skill, [(0.02, _unlock()), (0.04, _fg("A.exe"))])
        assert len(rec.calls) == 1
        title, body, actions = rec.calls[0]
        assert title == "knowbot 开工组合" and len(actions) == 2  # 拉起/不用

        row = memory.conn.execute(
            "SELECT id, exec_mode, reaction FROM action_log ORDER BY id DESC").fetchone()
        assert row["exec_mode"] == "suggest" and row["reaction"] is None
        r = apply_startup_action(memory, launcher, "launch", pref["id"], row["id"])
        assert r["ok"]
        assert launcher.calls == [["A.exe", "B.exe", "C.exe"]]     # S1 真拉起
        row2 = memory.get_action(row["id"])
        assert row2["reaction"] == "accept"
        assert row2["quadrant"] == "correct_detection"
        store.close()
    asyncio.run(main())


def test_s4_silence_records_missed_need(tmp_path):
    """手动开 ≥60% → 彻底沉默 + missed_need 记账（升档燃料）。"""
    async def main():
        store, memory, pref = _setup(tmp_path, conf=0.55)
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        rec = _Rec()
        skill = StartupSkill(bus, memory, FakeLauncher(), rec, P)
        skill.start(loop)
        await _drive(bus, skill, [(0.02, _unlock()), (0.04, _fg("A.exe")),
                                  (0.05, _fg("B.exe"))])           # 2/3 ≥ 0.6
        assert rec.calls == []                                     # 不弹
        row = memory.conn.execute(
            "SELECT reaction, quadrant, reaction_detail FROM action_log"
            " ORDER BY id DESC").fetchone()
        assert row["reaction"] == "missed_need"
        assert json.loads(row["reaction_detail"])["rule"] == "S4"
        value = json.loads(memory.get_pref(pref["id"])["value"])
        assert value["missed_count"] == 1
        store.close()
    asyncio.run(main())


def test_s2_dismiss_and_double_dismiss_demotes(tmp_path):
    """S2：点[不用]→纠正；连点两次 → 降回纯观察（conf 0.30）。"""
    async def main():
        store, memory, pref = _setup(tmp_path, conf=0.55)
        launcher = FakeLauncher()
        aid = memory.log_action("startup", None, pref["id"],
                                {"type": "suggest", "combo": []}, "suggest", 1.0)
        r = apply_startup_action(memory, launcher, "dismiss", pref["id"], aid)
        assert r["ok"] and abs(r["confidence_after"] - 0.55 * 0.8) < 1e-9
        aid2 = memory.log_action("startup", None, pref["id"],
                                 {"type": "suggest", "combo": []}, "suggest", 2.0)
        apply_startup_action(memory, launcher, "dismiss", pref["id"], aid2)
        assert memory.get_pref(pref["id"])["confidence"] == 0.30   # 回纯观察
        store.close()
    asyncio.run(main())


def test_exec_tier_auto_launches_missing_only(tmp_path):
    """执行档（explicit 0.9 + auto）：只拉缺失成员，静默执行。"""
    async def main():
        store, memory, pref = _setup(tmp_path, conf=0.90, auto=True)
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        rec = _Rec()
        launcher = FakeLauncher()
        skill = StartupSkill(bus, memory, launcher, rec, P)
        skill.start(loop)
        await _drive(bus, skill, [(0.02, _unlock()), (0.04, _fg("A.exe")),
                                  (0.05, _fg("B.exe"))])   # 已手动开 2/3
        assert rec.calls == []                        # 执行档不打扰
        assert launcher.calls == [["C.exe"]]          # 只拉缺的
        row = memory.conn.execute(
            "SELECT exec_mode FROM action_log ORDER BY id DESC").fetchone()
        assert row["exec_mode"] == "silent"
        store.close()
    asyncio.run(main())


def test_auto_op_requires_missed_gate(tmp_path):
    """auto 升档门：missed_need <3 拒绝开通自动拉起（入场券 §8）。"""
    async def main():
        store, memory, pref = _setup(tmp_path, conf=0.55)
        launcher = FakeLauncher()
        aid = memory.log_action("startup", None, pref["id"],
                                {"type": "suggest", "combo": []}, "suggest", 1.0)
        r = apply_startup_action(memory, launcher, "auto", pref["id"], aid)
        assert r["ok"] is False and "未达标" in r["error"]
        from knowbot.skills.startup import _bump_value_counter
        for _ in range(3):
            _bump_value_counter(memory, pref["id"], "missed_count")
        aid2 = memory.log_action("startup", None, pref["id"],
                                 {"type": "suggest", "combo": []}, "suggest", 2.0)
        r2 = apply_startup_action(memory, launcher, "auto", pref["id"], aid2)
        assert r2["ok"] and r2["auto"]
        row = memory.get_pref(pref["id"])
        assert row["source"] == "explicit" and row["confidence"] == 0.9
        assert json.loads(row["value"])["auto"] is True
        store.close()
    asyncio.run(main())
