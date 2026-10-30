"""域名传感器（app.web/host，中间地带第一批候选，docs/authorized-sensors.md）。

**截断纪律（本模块的宪法，写入代码而非注释）**：
- 仅当前台是白名单浏览器时读地址栏；读到的文本只进 `truncate_host`
  一个函数，出函数后系统里只剩 host——完整 URL 永不落库、永不进日志
  （含 debug 日志的格式化参数）；
- 窗口标题只做私密窗布尔判定（InPrivate/Incognito/隐私浏览），
  标题文本即刻丢弃；
- 未授权（authorized() 为假）= 不读不采，无任何痕迹。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from urllib.parse import urlsplit

from ..bus import EventBus
from ..envelope import Event
from .base import Sensor

log = logging.getLogger(__name__)

PRIVATE_MARKERS = ("inprivate", "incognito", "private browsing", "隐私浏览")


def truncate_host(text: str) -> str | None:
    """地址栏文本 → host。非 URL（搜索词）、内页（edge://）、空值 → None。

    纪律：这是全系统唯一接触完整文本的函数；调用方拿到返回值后，
    完整文本不复存在。函数自身不产生任何日志。
    """
    try:
        parts = urlsplit(text.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.netloc or "").lower()
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]  # 去 userinfo 与端口
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _is_private(title: str | None) -> bool:
    """窗口标题 → 私密窗布尔（标题即刻丢弃，只留布尔）。"""
    if not title:
        return False
    t = title.lower()
    return any(m in t for m in PRIVATE_MARKERS)


def _exe_of_pid(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            import os
            return os.path.basename(buf.value)
    finally:
        k32.CloseHandle(h)
    return None


def read_browser_location(hwnd: int) -> tuple[str, bool] | None:
    """UIA 读地址栏文本与私密窗标记（在一次性线程内执行，MTA）。

    返回 (地址栏文本|None, 私密窗)；调用方须立即对文本做 truncate_host。
    读取失败返回 None——本函数不抛出、不打含内容的日志。
    """
    out: dict = {}

    def _work():
        try:
            import comtypes
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError:
                pass
            try:
                import comtypes.client
                comtypes.client.GetModule("UIAutomationCore.dll")
                from comtypes.gen.UIAutomationClient import (
                    CUIAutomation, IUIAutomation, TreeScope_Descendants,
                    UIA_ControlTypePropertyId, UIA_EditControlTypeId,
                    UIA_NamePropertyId, UIA_ValueValuePropertyId)
                uia = comtypes.client.CreateObject(
                    CUIAutomation, interface=IUIAutomation)
                el = uia.ElementFromHandle(hwnd)
                if el is None:
                    return
                out["private"] = _is_private(el.CurrentName)  # 标题即弃
                # 只按无障碍名称精确匹配地址栏（Edge/Chrome 中英文 UI）。
                # 禁止"任意 Edit"兜底——网页内输入框也是 Edit，读到即内容违规
                # （2026-10-29 真机冒烟实证）。找不到 = 本窗不读。
                for try_name in ("地址和搜索栏", "Address and search bar"):
                    cond = uia.CreateAndCondition(
                        uia.CreatePropertyCondition(
                            UIA_ControlTypePropertyId, UIA_EditControlTypeId),
                        uia.CreatePropertyCondition(
                            UIA_NamePropertyId, try_name))
                    edit = el.FindFirst(TreeScope_Descendants, cond)
                    if edit is not None:
                        out["text"] = edit.GetPropertyValue(
                            UIA_ValueValuePropertyId) or ""
                        return
            finally:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass
        except Exception:
            log.debug("UIA 地址栏读取失败", exc_info=True)  # 无内容字段

    t = threading.Thread(target=_work, daemon=True, name="knowbot-webhost-read")
    t.start()
    t.join(timeout=3)
    if "text" not in out and "private" not in out:
        return None
    return out.get("text"), out.get("private", False)


class WebHostSensor(Sensor):
    """前台切到白名单浏览器 → 读地址栏 → 截断 host → 变化才发布。"""

    name = "app.web"

    def __init__(self, bus: EventBus, browsers: set[str],
                 authorized, reader=read_browser_location) -> None:
        self._bus = bus
        self._browsers = {b.lower() for b in browsers}
        self._authorized = authorized   # () -> bool（未授权不读不采）
        self._reader = reader           # 测试注入
        self._last_host: str | None = None

    def start(self, loop) -> None:
        self._bus.subscribe("app.foreground", self._on_fg)

    def stop(self) -> None:
        pass

    def _on_fg(self, ev: Event) -> None:
        if ev.type != "changed" or not self._authorized():
            return
        exe = (ev.payload.get("exe") or "").lower()
        pid = ev.payload.get("pid")
        if exe not in self._browsers or not pid:
            return
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return
        got = self._reader(hwnd)
        if got is None:
            return
        text, private = got
        if private:
            return  # 私密窗口：不读不记（含此前 last_host 不动）
        host = truncate_host(text or "")
        if not host or host == self._last_host:
            return
        self._last_host = host
        self._bus.publish(Event(self.name, "host",
                                 {"host": host, "browser": exe}))
