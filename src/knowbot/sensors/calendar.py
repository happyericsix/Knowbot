"""日历忙闲传感器（calendar.busy，中间地带第一批候选）。

授权：config.toml 提供 ICS 订阅 URL 本身即授权（daemon 启动时自动
登记，曾被面板撤销则尊重撤销标记）。解析纪律：只取 VEVENT 的
DTSTART/DTEND，标题/描述/参与人/地点在解析器里直接丢弃——
输出里结构上不可能存在它们。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from ..bus import EventBus
from ..envelope import Event
from .base import Sensor

log = logging.getLogger(__name__)

LOOKAHEAD_DAYS = 7


def _parse_dt(value: str) -> datetime | None:
    """ICS 时间值 → naive 本地 datetime。支持 ..T..Z / ..T.. / ..（日期）。"""
    value = value.strip()
    try:
        if "T" in value:
            body = value.rstrip("Z")
            dt = datetime.strptime(body, "%Y%m%dT%H%M%S")
            if value.endswith("Z"):
                dt = dt.replace(tzinfo=__import__("datetime").timezone.utc)
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None


def parse_ics(text: str) -> list[dict]:
    """ICS 文本 → 忙闲块列表（纯函数）。

    只保留 start/end/all_day 三键；SUMMARY 等内容字段在行级即被忽略
    （根本没有变量承接它们）。
    """
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:   # RFC5545 折行续接
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    out: list[dict] = []
    in_event, start, end = False, None, None
    for line in lines:
        tag = line.split(":", 1)[0].split(";", 1)[0].upper()
        if tag == "BEGIN" and line.upper().endswith("VEVENT"):
            in_event, start, end = True, None, None
        elif tag == "END" and line.upper().endswith("VEVENT"):
            if in_event and start:
                out.append({"start": start,
                            "end": end or start,
                            "all_day": "T" not in (start or "")})
            in_event = False
        elif in_event and tag == "DTSTART":
            start = line.split(":", 1)[1] if ":" in line else None
        elif in_event and tag == "DTEND":
            end = line.split(":", 1)[1] if ":" in line else None
    return out


def busy_snapshot(text: str, today: date) -> dict:
    """忙闲快照：今日时段 + 未来 7 天事件数 + 全天日期。"""
    blocks = []
    all_day: list[str] = []
    week = 0
    horizon = today + timedelta(days=LOOKAHEAD_DAYS)
    for ev in parse_ics(text):
        s, e = _parse_dt(ev["start"]), _parse_dt(ev["end"])
        if s is None:
            continue
        if ev.get("all_day"):
            if today <= s.date() < horizon:
                week += 1
            if s.date() == today:
                all_day.append(s.date().isoformat())
            continue
        if today <= s.date() < horizon:
            week += 1
        if s.date() == today:
            blocks.append([s.strftime("%H:%M"),
                           (e or s).strftime("%H:%M")])
    blocks.sort()
    return {"today": blocks, "all_day": all_day, "week_event_count": week,
            "lookahead_days": LOOKAHEAD_DAYS}


class CalendarSensor(Sensor):
    """周期拉取 ICS → 忙闲快照（内容变化才发布）。

    上下文时效保证（2026-10-30 架构分析）：忙闲是状态不是偏好——
    周期刷新（默认 30 分钟）之外，`fresh_snapshot(max_age_s)` 供查询方
    取数：缓存过期就先刷新再返回，绝不给陈旧的"今晚没安排"。
    """

    name = "calendar.busy"

    def __init__(self, bus: EventBus, ics_url: str, authorized,
                 refresh_hours: float = 0.5, fetch=None) -> None:
        self._bus = bus
        self._url = ics_url
        self._authorized = authorized
        self._refresh_s = refresh_hours * 3600
        self._fetch = fetch or self._default_fetch
        self._task: asyncio.Task | None = None
        self._last_payload: dict | None = None
        self._last_fetch_mono: float = 0.0

    def start(self, loop) -> None:
        self._task = loop.create_task(self._run(), name="sensor-calendar")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @staticmethod
    def _default_fetch(url: str) -> str:
        import urllib.request
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")

    async def _refresh(self) -> dict | None:
        """拉取→解析→变化才发布；更新新鲜度时间戳。"""
        import time as _t
        if not self._authorized():
            return None
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, self._fetch, self._url)
        self._last_fetch_mono = _t.monotonic()
        snap = busy_snapshot(text, date.today())
        if snap != self._last_payload:
            self._last_payload = snap
            self._bus.publish(Event(self.name, "busy", snap))
        return snap

    async def fresh_snapshot(self, max_age_s: float = 900.0) -> dict | None:
        """带 TTL 的忙闲快照：缓存过期先刷新再返回（查询方入口）。"""
        import time as _t
        if (self._last_payload is None
                or _t.monotonic() - self._last_fetch_mono > max_age_s):
            try:
                await self._refresh()
            except Exception:
                log.warning("按需刷新失败，返回现有缓存", exc_info=True)
        return self._last_payload

    async def _run(self) -> None:
        while True:
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("日历忙闲拉取失败", exc_info=True)
            await asyncio.sleep(self._refresh_s)
