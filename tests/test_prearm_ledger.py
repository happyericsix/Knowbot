"""pre-arm 台账落账测试：反射动作 → action_log（台账不可豁免）。"""
import asyncio
import json

from knowbot.bus import EventBus
from knowbot.envelope import Event
from knowbot.skills.prearm import PrearmLedger
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore

PREV = "{0.0.0.00000000}.{302f05c4-prev}"
NEW = "{0.0.0.00000000}.{59dd6575-new}"


def _setup(tmp_path):
    store = EventStore(tmp_path / "pa.db", batch_size=10_000, flush_interval=0.05)
    memory = MemoryStore(store._conn)
    return store, memory


def test_prearm_event_lands_in_ledger_with_trace(tmp_path):
    """prearm 事件 → 台账行（silent/zero/undo=前值）+ 溯源回填到 default_changed。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        bus.subscribe("", store.append)           # 生产装配：store 先订阅
        flusher = asyncio.create_task(store.run())
        ledger = PrearmLedger(bus, memory, backfill_delay_s=0.2)  # > flush 周期
        ledger.start(asyncio.get_running_loop())
        runner = asyncio.create_task(bus.run())

        bus.publish(Event("audio.device", "default_changed",
                          {"device_id": NEW, "flow": "render", "role": "multimedia"}))
        bus.publish(Event("audio.device", "prearm",
                          {"device_id": PREV, "new_default": NEW,
                           "before": 0.6, "after": 0.2}))
        await asyncio.sleep(0.5)                  # 落库 flush + 延迟回填都完成
        runner.cancel(); flusher.cancel()
        await asyncio.gather(runner, flusher, return_exceptions=True)

        row = memory.conn.execute(
            "SELECT skill, exec_mode, risk, action, undo_payload, trigger_event_id"
            " FROM action_log ORDER BY id DESC").fetchone()
        assert row["skill"] == "prearm"
        assert row["exec_mode"] == "silent" and row["risk"] == "zero"
        a = json.loads(row["action"])
        assert a == {"type": "prearm", "device_id": PREV, "before": 0.6, "after": 0.2}
        assert json.loads(row["undo_payload"]) == {"restore": 0.6}
        ev = memory.conn.execute(
            "SELECT id FROM event_raw WHERE type='default_changed'").fetchone()
        assert row["trigger_event_id"] == ev["id"]  # 溯源链闭合
        store.close()

    asyncio.run(main())


def test_non_prearm_events_ignored(tmp_path):
    """default_changed 本身不落台账（它不是动作）。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        bus.subscribe("", store.append)
        runner = asyncio.create_task(bus.run())
        ledger = PrearmLedger(bus, memory)
        ledger.start(asyncio.get_running_loop())
        bus.publish(Event("audio.device", "default_changed",
                          {"device_id": NEW, "flow": "render", "role": "multimedia"}))
        await asyncio.sleep(0.1)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 0
        store.close()

    asyncio.run(main())
