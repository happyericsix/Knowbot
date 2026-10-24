"""Win32 消息窗口传感器：一个消息泵线程承载三路信号。

- 隐藏顶层窗口收广播：WM_POWERBROADCAST（电源/睡眠/唤醒）、
  WM_WTSSESSION_CHANGE（锁屏/解锁/登录…）
- 同线程 SetWinEventHook(EVENT_SYSTEM_FOREGROUND)：前台应用切换
- 唤醒时发布 system.clock_reconcile —— 回放器靠它切分 mono 时轴

隐私：只记 exe 名与 pid，不取窗口标题（内容红线）。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes

from ..bus import EventBus
from ..envelope import Event, Sensor as SensorNS, now_wall
from .base import Sensor

log = logging.getLogger(__name__)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

WTS_REASON = {
    1: "console_connect", 2: "console_disconnect", 3: "remote_connect",
    4: "remote_disconnect", 5: "session_logon", 6: "session_logoff",
    7: "session_lock", 8: "session_unlock", 9: "remote_control",
}

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
    wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_wtsapi32 = ctypes.windll.wtsapi32

# 显式原型：64 位下句柄若按默认 c_int 截断会损坏句柄
_user32.SetWinEventHook.restype = wintypes.HANDLE
_user32.SetWinEventHook.argtypes = [
    wintypes.UINT, wintypes.UINT, wintypes.HMODULE,
    WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
_user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DefWindowProcW.restype = wintypes.LPARAM
_user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]


class _WNDCLASSW(ctypes.Structure):
    """ctypes.wintypes 在 3.12 前没有窗口类结构体，此处自定义。"""

    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class Win32MessageSensor(Sensor):
    name = "system.power"  # 实际发布 system.power / system.session / app.foreground 等多个命名空间

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._hook = None
        self._wndproc_ref = None     # 防 GC：ctypes 回调必须持强引用（勿与方法同名）
        self._wineventproc_ref = None  # 同上
        self._suspend_wall: str | None = None

    def start(self, loop) -> None:
        self._loop = loop
        self._thread = threading.Thread(
            target=self._run, name="knowbot-winmsg", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        t = self._thread
        if t is not None and t.is_alive() and self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            t.join(timeout=5)
        self._thread = None

    def _emit(self, sensor: str, etype: str, payload: dict) -> None:
        try:
            self._bus.publish_threadsafe(Event(sensor, etype, payload))
        except Exception:
            log.exception("发布事件失败 sensor=%s type=%s", sensor, etype)

    # ---------- 线程体 ----------

    def _run(self) -> None:
        hinstance = _kernel32.GetModuleHandleW(None)
        try:
            wc = _WNDCLASSW()
            self._wndproc_ref = WNDPROC(self._wndproc)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinstance
            wc.lpszClassName = "knowbot_hidden_window"
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError()

            self._hwnd = _user32.CreateWindowExW(
                0, "knowbot_hidden_window", "knowbot", 0,
                0, 0, 0, 0, None, None, hinstance, 0)
            if not self._hwnd:
                raise ctypes.WinError()

            if not _wtsapi32.WTSRegisterSessionNotification(self._hwnd, 0):
                log.warning("WTSRegisterSessionNotification 失败（会话事件不可用）")

            self._wineventproc_ref = WINEVENTPROC(self._on_foreground)
            self._hook = _user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                0, self._wineventproc_ref, 0, 0, WINEVENT_OUTOFCONTEXT)
            if not self._hook:
                log.warning("SetWinEventHook 失败（前台事件不可用）")

            self._emit(SensorNS.SENSOR_STATUS, "started",
                       {"sensor": self.name, "hwnd": self._hwnd})

            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log.exception("win32 消息传感器初始化失败")
            self._emit(SensorNS.SENSOR_STATUS, "unavailable",
                       {"sensor": self.name, "reason": repr(e)})
        finally:
            self._teardown()

    def _teardown(self) -> None:
        if self._hook:
            try:
                _user32.UnhookWinEvent(self._hook)
            except Exception:
                pass
            self._hook = None
        if self._hwnd:
            try:
                _wtsapi32.WTSUnRegisterSessionNotification(self._hwnd)
            except Exception:
                pass
            self._hwnd = 0

    # ---------- 窗口过程（消息线程） ----------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            return self._handle_message(hwnd, msg, wparam, lparam)
        except Exception:
            log.exception("WNDPROC 异常 msg=0x%X", msg)
            return 0

    def _handle_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                self._suspend_wall = now_wall()
                self._emit(SensorNS.POWER, "suspend", {})
                return 1  # WM_POWERBROADCAST 应返回 TRUE
            if wparam == PBT_APMRESUMEAUTOMATIC:
                self._emit_resume("resume")
                return 1
            if wparam == PBT_APMRESUMESUSPEND:
                self._emit_resume("resume_user")
                return 1
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        if msg == WM_WTSSESSION_CHANGE:
            self._emit(SensorNS.SESSION, WTS_REASON.get(wparam, f"unknown_{wparam}"), {})
            return 0
        if msg == WM_CLOSE:
            _user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _emit_resume(self, etype: str) -> None:
        """唤醒：电源事件 + 时钟对账事件（回放器据此切分 mono 时轴）。"""
        payload = {
            "wall_before_suspend": self._suspend_wall,
            "wall_after_resume": now_wall(),
            "mono_after_resume": time.monotonic(),
        }
        self._suspend_wall = None
        self._emit(SensorNS.POWER, etype, {})
        self._emit(SensorNS.CLOCK, "reconcile", payload)

    # ---------- 前台切换（消息线程） ----------

    def _on_foreground(self, hhook, event, hwnd, id_object, id_child, thread_id, ts_ms):
        try:
            if not hwnd:
                return
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            self._emit(SensorNS.FOREGROUND, "changed", {
                "exe": self._exe_name(pid.value),
                "pid": pid.value or None,
            })
        except Exception:
            log.exception("前台事件处理失败")

    @staticmethod
    def _exe_name(pid: int) -> str | None:
        if not pid:
            return None
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return None
        finally:
            _kernel32.CloseHandle(h)
