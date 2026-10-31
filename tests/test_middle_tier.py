"""中间地带传感器测试（§21）：截断纪律 / 授权门 / 撤销即焚 / ICS 解析。

截断纪律是宪法级用例：完整 URL 与内容字段必须在结构上不可能出现在
任何产物里（不是"约定不出现"，是"没有变量承接"）。
"""
import asyncio
import json

import pytest

from knowbot.bus import EventBus
from knowbot.envelope import Event
from knowbot.sensors.auth import MIDDLE_TIER, grant, is_authorized, revoke
from knowbot.sensors.calendar import busy_snapshot, parse_ics
from knowbot.sensors.webhost import WebHostSensor, truncate_host
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:与老板的秘密会议
DTSTART:20261102T183000
DTEND:20261102T191500
END:VEVENT
BEGIN:VEVENT
SUMMARY:出行
DTSTART;VALUE=DATE:20261103
DTEND;VALUE=DATE:20261104
END:VEVENT
BEGIN:VEVENT
SUMMARY:线上会
DTSTART:20261102T093000Z
DTEND:20261102T100000Z
END:VEVENT
END:VCALENDAR
"""


# ---------- 截断纪律（宪法级） ----------

def test_truncate_host_discards_everything_but_host():
    assert truncate_host(
        "https://douban.com/movie/subject/1292052/?from=推荐&q=科幻 预告片"
    ) == "douban.com"
    assert truncate_host("https://www.Bing.com:443/search?q=私有查询") == "bing.com"
    assert truncate_host("how to cook pasta") is None        # 搜索词非 URL
    assert truncate_host("edge://settings/privacy") is None  # 内页
    assert truncate_host("") is None
    # 纪律：返回值类型上只可能是 host 或 None——不含路径/查询的结构性保证
    for text in ("https://a.com/x?q=1", "https://b.com", "随便打的字"):
        r = truncate_host(text)
        assert r is None or ("/" not in r and "?" not in r and "=" not in r)


def test_private_window_marker():
    from knowbot.sensors.webhost import _is_private
    assert _is_private("某些标题 - InPrivate - Microsoft Edge") is True
    assert _is_private("Movie trailers - Google Chrome") is False
    assert _is_private(None) is False


# ---------- 授权门（未授权 = 不读不采） ----------

def _mk_sensor(bus, auth, reader):
    return WebHostSensor(bus, {"msedge.exe"}, auth, reader=reader)


def test_unauthorized_never_reads_or_publishes():
    async def main():
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        events = []
        bus.subscribe("app.web", events.append)
        runner = asyncio.create_task(bus.run())
        reads = []
        sensor = _mk_sensor(bus, lambda: False,
                            lambda hwnd: reads.append(hwnd) or ("https://x.com/a", False))
        sensor.start(loop)
        bus.publish(Event("app.foreground", "changed",
                          {"exe": "msedge.exe", "pid": 123}))
        await asyncio.sleep(0.1)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        assert reads == [] and events == []      # 未授权：读取都未发生

    asyncio.run(main())


def test_authorized_publishes_host_deduped():
    async def main():
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        events = []
        bus.subscribe("app.web", events.append)
        runner = asyncio.create_task(bus.run())
        seq = iter(["https://douban.com/movie/preview?id=9",
                    "https://douban.com/other/page"])
        sensor = _mk_sensor(bus, lambda: True,
                            lambda hwnd: (next(seq), False))
        sensor.start(loop)
        for _ in range(2):
            bus.publish(Event("app.foreground", "changed",
                              {"exe": "msedge.exe", "pid": 1}))
            await asyncio.sleep(0.05)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        hosts = [e.payload["host"] for e in events]
        assert hosts == ["douban.com"]            # 同 host 去重；无路径无查询
        assert all("id=" not in h and "/" not in h for h in hosts)

    asyncio.run(main())


def test_private_window_publishes_nothing():
    async def main():
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        events = []
        bus.subscribe("app.web", events.append)
        runner = asyncio.create_task(bus.run())
        sensor = _mk_sensor(bus, lambda: True,
                            lambda hwnd: ("https://bank.com/pay", True))
        sensor.start(loop)
        bus.publish(Event("app.foreground", "changed",
                          {"exe": "msedge.exe", "pid": 1}))
        await asyncio.sleep(0.1)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        assert events == []                        # 私密窗零产出

    asyncio.run(main())


# ---------- ICS 解析（内容字段结构性不存在） ----------

def test_ics_parse_keeps_only_times():
    import datetime
    blocks = parse_ics(ICS)
    assert len(blocks) == 3
    snap = busy_snapshot(ICS, datetime.date(2026, 11, 2))
    # Z 时间转本地（+08:00 → 17:30）；本地时间事件原样
    assert snap["today"] == [["17:30", "18:00"], ["18:30", "19:15"]]
    assert snap["week_event_count"] == 3
    # 纪律：标题字段在结构上不存在
    blob = json.dumps(snap) + json.dumps(blocks)
    assert "会议" not in blob and "SUMMARY" not in blob and "老板" not in blob


# ---------- 授权机制（授予/撤销即焚/治理留痕） ----------

def test_grant_revoke_and_burn(tmp_path):
    store = EventStore(tmp_path / "a.db")
    memory = MemoryStore(store._conn)
    assert "app.web" in MIDDLE_TIER
    assert is_authorized(memory, "app.web") is False

    grant(memory, "app.web")
    assert is_authorized(memory, "app.web") is True
    store.append(Event("app.web", "host", {"host": "douban.com"}))
    store.append(Event("system.heartbeat", "beat", {}))
    store._flush()

    n = revoke(memory, "app.web")
    assert n == 1                                      # 只焚毁该传感器
    assert is_authorized(memory, "app.web") is False
    left = [r["sensor"] for r in memory.conn.execute(
        "SELECT sensor FROM event_raw")]
    assert left == ["system.heartbeat"]                # 其他传感器不动
    grant(memory, "app.web")                           # 重新授权可再采集
    assert is_authorized(memory, "app.web") is True
    store.close()


def test_calendar_fresh_snapshot_ttl():
    """上下文时效：fresh_snapshot 缓存期内不重复拉取，过期就地刷新。"""
    from knowbot.sensors.calendar import CalendarSensor

    async def main():
        loop = asyncio.get_running_loop()
        bus = EventBus(); bus.bind(loop)
        events = []
        bus.subscribe("calendar.busy", events.append)
        runner = asyncio.create_task(bus.run())
        fetches = []
        import datetime as _dt
        d = _dt.date.today().strftime("%Y%m%d")     # 动态日期：忙闲按"今日"过滤
        ics_iter = iter([
            f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:{d}T180000\n"
            f"DTEND:{d}T190000\nEND:VEVENT\nEND:VCALENDAR",
            "BEGIN:VCALENDAR\nEND:VCALENDAR",   # 第二次拉取：今晚安排被取消
        ])
        def fake_fetch(url):
            fetches.append(url)
            return next(ics_iter)
        sensor = CalendarSensor(bus, "https://x/ics", lambda: True,
                                refresh_hours=999.0, fetch=fake_fetch)
        snap1 = await sensor.fresh_snapshot(max_age_s=900.0)
        assert snap1["today"] == [["18:00", "19:00"]] and len(fetches) == 1
        snap2 = await sensor.fresh_snapshot(max_age_s=900.0)
        assert len(fetches) == 1                     # TTL 内：用缓存不拉取
        sensor._last_fetch_mono -= 1000.0            # 模拟缓存过期
        snap3 = await sensor.fresh_snapshot(max_age_s=900.0)
        assert len(fetches) == 2                     # 过期：就地刷新
        assert snap3["today"] == []                  # 拿到的是新状态
        await asyncio.sleep(0.05)                    # 让总线分发第二事件
        assert [e.payload["today"] for e in events] == [
            [["18:00", "19:00"]], []]                # 两次变化各发一条
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(main())


# ---------- 演练模式（rehearsal_mode，2026-10-30 环境分级） ----------

def test_fake_launch_executor_zero_side_effects():
    from knowbot.executors.launch import FakeLaunchExecutor
    ex = FakeLaunchExecutor()
    r = ex.launch(["A.exe", "B.exe"])
    assert r["rehearsal"] is True
    assert [i["pid"] for i in r["launched"]] == [-1, -1]  # 无真实 pid
    assert ex.calls == [["A.exe", "B.exe"]]
    assert ex.undo(r["launched"]) == 0                     # 无事可撤


def test_rehearsal_config_default_off():
    from knowbot.config import Config
    assert Config().rehearsal_mode is False
