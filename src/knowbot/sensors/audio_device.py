"""音频设备传感器：旗舰场景（耳机插拔）的感知基础。

独立 STA 线程：CoInitializeEx(APARTMENTTHREADED) → 注册
IMMNotificationClient 回调 → 初始快照 → GetMessage 消息泵。

两个经典坑的对策（方案 §2.3）：
1. COM 回调对象被 GC 静默失效 —— 实例属性持有强引用；
2. STA 线程不泵消息收不到回调 —— GetMessageW 循环。

隐私：只记端点 ID / 友好名 / form factor / 流向 / 角色，不碰音频内容。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes

from ..bus import EventBus
from ..envelope import Event, Sensor as SensorNS
from ..executors.volume import (
    AGENT_CONTEXT_GUID, decide_prearm, endpoint_volume, get_master_scalar,
    prearm_allowed, set_master_scalar,
)
from .base import Sensor

log = logging.getLogger(__name__)

WM_QUIT = 0x0012
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"

# 音量通知去抖计时器：挂在 message-only 窗口上（窗口计时器的 TimerProc 由
# DispatchMessage 可靠分发；NULL-hwnd 线程计时器的回调不会到达——实测结论）
_TIMERPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.DWORD)
_WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
HWND_MESSAGE = wintypes.HWND(-3)
VOL_TIMER_ID = 1
REARM_TIMER_ID = 2     # 默认设备切换后的延迟重注册（避开 PnP 抖动窗口）
SENSOR_TICK_ID = 3     # STA 线程存活心跳（诊断"线程静默死亡"用）
SENSOR_TICK_MS = 60_000

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.SetTimer.restype = ctypes.c_size_t
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, _TIMERPROC]
_user32.KillTimer.restype = wintypes.BOOL
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DefWindowProcW.restype = wintypes.LPARAM
_user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


class _MSGWNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]

DEVICE_STATES = {1: "active", 2: "disabled", 4: "not_present", 8: "unplugged"}
FLOWS = {0: "render", 1: "capture", 2: "all"}
ROLES = {0: "console", 1: "multimedia", 2: "communications"}
FORM_FACTORS = {
    0: "remote_network_device", 1: "speakers", 2: "line_level", 3: "headphones",
    4: "microphone", 5: "headset", 6: "handset", 7: "unknown_digital_passthrough",
    8: "spdif", 9: "hdmi", 10: "unknown_form_factor",
}

# pycaw 未导出的属性键，此处自定义（Windows Core Audio property key）
_PKEY_DEVICE_FRIENDLY_NAME = ("{a45c254e-df1c-4efd-8020-67d146a850e0}", 14)
_PKEY_ENDPOINT_FORM_FACTOR = ("{1da5d803-d492-4edd-8c23-e0c0ffee7f0e}", 0)


class AudioDeviceSensor(Sensor):
    name = "audio.device"

    def __init__(self, bus: EventBus, *, watch_volume: bool = True,
                 vol_settle_s: float = 1.5, enable_prearm: bool = False,
                 prearm_safe_scalar: float = 0.2,
                 prearm_only_lower: bool = True,
                 prearm_cooldown_s: float = 300.0,
                 is_paused=None) -> None:
        self._bus = bus
        self._thread: threading.Thread | None = None
        self._client = None    # 防 GC：COMObject 回调对象强引用
        self._ptr = None       # 注册用的接口指针，防 GC
        self._enumerator = None  # 防 GC：枚举器强引用
        # Phase 1 ①：音量观察 + pre-arm
        self._watch_volume = watch_volume
        self._vol_settle_ms = max(200, int(vol_settle_s * 1000))
        self._enable_prearm = enable_prearm
        self._prearm_safe = prearm_safe_scalar
        self._prearm_only_lower = prearm_only_lower
        self._prearm_cooldown_s = prearm_cooldown_s
        self._prearm_ts: dict[str, float] = {}  # 设备 → 上次布防时刻（see-saw 防护）
        # 全局暂停探针（STA 线程调用；实现方须自备连接，不共享 store 连接）
        self._is_paused = is_paused
        self._last_default_render: str | None = None
        self._vol_cb = None        # 防 GC：音量回调 COMObject
        self._vol_cb_ptr = None    # 注册用接口指针
        self._vol_endpoint = None  # 当前注册了回调的 IAudioEndpointVolume
        self._vol_device_id: str | None = None
        self._vol_burst = 0
        self._vol_last = None      # 最近一次通知 (scalar, muted, actor)
        self._vol_prev_scalar = None  # 上次静止值（下次事件的 from）
        self._vol_timer_rid = 0
        self._vol_timerproc_ref = None
        self._rearm_timerproc_ref = None
        self._tick_timerproc_ref = None
        self._vol_hwnd = 0         # message-only 窗口（承载音量去抖计时器）
        self._vol_wndproc_ref = None

    def start(self, loop) -> None:
        self._loop = loop
        self._thread = threading.Thread(
            target=self._run_sta, name="knowbot-audio-sta", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        t = self._thread
        if t is not None and t.is_alive():
            # CoInitialize 后线程消息队列即存在；WM_QUIT 让 GetMessage 泵退出
            ctypes.windll.user32.PostThreadMessageW(t.native_id, WM_QUIT, 0, 0)
            t.join(timeout=5)
        self._thread = None

    # ---------- STA 线程体 ----------

    def _run_sta(self) -> None:
        try:
            import comtypes
            from pycaw.pycaw import IMMDeviceEnumerator, IMMNotificationClient
        except Exception as e:
            log.warning("音频传感器不可用（缺依赖 comtypes/pycaw）：%r", e)
            self._report_unavailable(repr(e))
            return

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except Exception as e:
            self._report_unavailable(f"CoInitializeEx 失败: {e!r}")
            return

        try:
            self._enumerator = comtypes.CoCreateInstance(
                comtypes.GUID(CLSID_MMDeviceEnumerator),
                IMMDeviceEnumerator,
                comtypes.CLSCTX_INPROC_SERVER,
            )
            self._client = _make_notify_client(self._on_com_event,
                                               on_default=self._on_default_changed)
            self._ptr = self._client.QueryInterface(IMMNotificationClient)
            self._enumerator.RegisterEndpointNotificationCallback(self._ptr)
            try:
                self._last_default_render = self._enumerator.GetDefaultAudioEndpoint(0, 1).GetId()
            except Exception:
                pass
            self._create_msg_window()
            self._vol_timerproc_ref = _TIMERPROC(self._on_vol_timer)
            self._rearm_timerproc_ref = _TIMERPROC(self._on_rearm_timer)
            self._tick_timerproc_ref = _TIMERPROC(self._on_sensor_tick)
            if self._vol_hwnd:
                _user32.SetTimer(self._vol_hwnd, SENSOR_TICK_ID, SENSOR_TICK_MS,
                                 self._tick_timerproc_ref)
            if self._watch_volume:
                self._register_volume_cb()
            self._snapshot()
            self._pump_messages()
        except Exception as e:
            log.exception("音频传感器运行失败")
            self._report_unavailable(repr(e))
        finally:
            self._unregister_volume_cb()
            if self._vol_timer_rid and self._vol_hwnd:
                try:
                    _user32.KillTimer(self._vol_hwnd, VOL_TIMER_ID)
                except Exception:
                    pass
                self._vol_timer_rid = 0
            if self._vol_hwnd:
                try:
                    _user32.DestroyWindow(self._vol_hwnd)
                except Exception:
                    pass
                self._vol_hwnd = 0
            if self._enumerator is not None and self._ptr is not None:
                try:
                    self._enumerator.UnregisterEndpointNotificationCallback(self._ptr)
                except Exception:
                    log.warning("反注册音频回调失败", exc_info=True)
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    def _pump_messages(self) -> None:
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        # Dispatch 必不可少：跨套间 COM 回调（音量通知来自音频服务进程）
        # 以消息形式到达本 STA 线程的隐藏 COM 窗口，不 Dispatch 则 OnNotify
        # 永远不会触发；SetTimer 的 TimerProc 同样依赖消息分发。
        # 0 = WM_QUIT；-1 = 错误。任何退出路径都留痕（防静默死亡无法诊断）。
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                log.warning("音频 STA 消息泵退出：GetMessage=%s（0=WM_QUIT, -1=错误）", r)
                break
            user32.DispatchMessageW(ctypes.byref(msg))

    @staticmethod
    def _default_wndproc(hwnd, msg, wp, lp):
        return _user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _on_rearm_timer(self, hwnd, msg, timer_id, dwtime) -> None:
        """默认设备切换后的延迟重注册：此刻已不在 COM 回调嵌套内，
        且 PnP 抖动多半已平息——避免对消失中的端点做跨进程 RPC。"""
        _user32.KillTimer(self._vol_hwnd, REARM_TIMER_ID)
        if self._watch_volume:
            self._register_volume_cb()

    def _on_sensor_tick(self, hwnd, msg, timer_id, dwtime) -> None:
        """STA 线程存活证明：每分钟一拍。缺失即线程死亡/卡死的可观测信号。"""
        self._bus.publish_threadsafe(Event(
            SensorNS.SENSOR_STATUS, "tick", {"sensor": self.name}))

    # ---------- 事件出口（均在 STA 线程内调用） ----------

    def _on_com_event(self, etype: str, fields: dict) -> None:
        payload = dict(fields)
        device_id = payload.pop("device_id", None)
        if device_id and etype != "device_removed":
            payload.update(self._device_meta_by_id(device_id))
        if device_id:
            payload["device_id"] = device_id
        self._bus.publish_threadsafe(Event(self.name, etype, payload))

    def _report_unavailable(self, reason: str) -> None:
        try:
            self._bus.publish_threadsafe(Event(self.name, "unavailable", {"reason": reason}))
        except Exception:
            pass

    # ---------- 快照与元信息 ----------

    def _snapshot(self) -> None:
        """启动时枚举全部端点各发一条 snapshot，作为回放与首见检测的基线。"""
        for flow in (0, 1):  # render / capture
            try:
                col = self._enumerator.EnumAudioEndpoints(flow, 0xF)  # 全部状态
                for i in range(col.GetCount()):
                    dev = col.Item(i)
                    payload = {
                        "origin": "snapshot",
                        "flow": FLOWS.get(flow),
                        "device_id": dev.GetId(),
                    }
                    payload.update(_device_meta(dev))
                    self._bus.publish_threadsafe(Event(self.name, "snapshot", payload))
            except Exception:
                log.exception("枚举音频端点失败 flow=%s", flow)

    def _device_meta_by_id(self, device_id: str) -> dict:
        try:
            dev = self._enumerator.GetDevice(device_id)
            return _device_meta(dev)
        except Exception:
            return {}

    # ---------- Phase 1 ①：默认切换钩子（pre-arm + 音量回调迁移） ----------

    def _on_default_changed(self, flow: int, role: int, new_id) -> None:
        """默认 render/multimedia 输出变化：本线程内同步处理（快路径）。

        音量回调的重注册【不在此处直接做】——此处仍在 COM 回调嵌套内，
        对刚消失端点的 RPC（反注册）在 PnP 拆除窗口可能挂死 STA 线程
        （2026-10-25 19:48 真实事故：拔出后音频事件全部静默消失）。
        改为 400ms 延迟定时器，出回调上下文后重注册。
        """
        if flow != 0 or role != 1 or not new_id:
            return
        prev, self._last_default_render = self._last_default_render, new_id
        if self._watch_volume and self._vol_hwnd:
            _user32.SetTimer(self._vol_hwnd, REARM_TIMER_ID, 400, self._rearm_timerproc_ref)
        # pre-arm：趁 prev 端点休眠布防（方案 §5：把竞态变成预先布防）。
        # 全局暂停同样约束反射层——暂停的语义是"agent 别动我的系统"。
        # see-saw 防护：快速来回切换时同设备冷却期内不重复布防
        # （实证 2026-10-27 23:10：75 秒 6 次布防写放大）。
        if not (self._enable_prearm and prev and prev != new_id):
            return
        if self._is_paused is not None and self._is_paused():
            return
        last = self._prearm_ts.get(prev)
        if not prearm_allowed(last, time.monotonic(), self._prearm_cooldown_s):
            return
        try:
            cur = get_master_scalar(self._enumerator, prev)
            target = decide_prearm(prev, new_id, cur, self._prearm_safe,
                                   self._prearm_only_lower)
            if target is None:
                return
            before = set_master_scalar(self._enumerator, target, prev)
            if before is not None:
                self._prearm_ts[prev] = time.monotonic()
                self._bus.publish_threadsafe(Event(self.name, "prearm", {
                    "device_id": prev, "new_default": new_id,
                    "before": round(before, 4), "after": round(target, 4),
                }))
        except Exception:
            log.warning("pre-arm 执行失败", exc_info=True)

    # ---------- 音量观察（IAudioEndpointVolumeCallback + 去抖） ----------

    def _register_volume_cb(self) -> None:
        from pycaw.pycaw import IAudioEndpointVolumeCallback

        # 反注册单独容错：对刚消失端点的 RPC 失败属预期（泄漏无害，
        # audiosrv 会在对象释放后自清）——绝不因它阻断新注册。
        if self._vol_endpoint is not None and self._vol_cb_ptr is not None:
            try:
                self._vol_endpoint.UnregisterControlChangeNotify(self._vol_cb_ptr)
            except Exception:
                log.debug("旧端点音量回调反注册失败（端点已消失，可忽略）")
        self._vol_endpoint = None
        self._vol_cb_ptr = None
        self._vol_cb = None
        self._vol_device_id = None
        try:
            dev_id = self._last_default_render
            if dev_id is None:
                dev_id = self._enumerator.GetDefaultAudioEndpoint(0, 1).GetId()
                self._last_default_render = dev_id
            vol = endpoint_volume(self._enumerator, dev_id)
            self._vol_cb = _make_volume_callback(self._on_volume_notify)
            self._vol_cb_ptr = self._vol_cb.QueryInterface(IAudioEndpointVolumeCallback)
            vol.RegisterControlChangeNotify(self._vol_cb_ptr)
            self._vol_endpoint = vol
            self._vol_device_id = dev_id
        except Exception:
            log.warning("音量回调注册失败（音量观察不可用）", exc_info=True)

    def _unregister_volume_cb(self) -> None:
        if self._vol_endpoint is not None and self._vol_cb_ptr is not None:
            try:
                self._vol_endpoint.UnregisterControlChangeNotify(self._vol_cb_ptr)
            except Exception:
                pass
        self._vol_endpoint = None
        self._vol_cb_ptr = None
        self._vol_cb = None
        self._vol_device_id = None

    def _create_msg_window(self) -> None:
        """在 STA 线程建 message-only 窗口：承载音量去抖计时器。"""
        def _default_proc(hwnd, msg, wp, lp):
            return _user32.DefWindowProcW(hwnd, msg, wp, lp)

        hinst = _kernel32.GetModuleHandleW(None)
        wc = _MSGWNDCLASS()
        self._vol_wndproc_ref = _WNDPROC(_default_proc)
        wc.lpfnWndProc = self._vol_wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = "knowbot_audio_msgwin"
        if not _user32.RegisterClassW(ctypes.byref(wc)):
            log.warning("音频消息窗口类注册失败", exc_info=True)
            return
        self._vol_hwnd = _user32.CreateWindowExW(
            0, "knowbot_audio_msgwin", "knowbot-audio", 0,
            0, 0, 0, 0, HWND_MESSAGE, None, hinst, 0)
        if not self._vol_hwnd:
            log.warning("音频消息窗口创建失败（音量观察不可用）", exc_info=True)

    def _on_volume_notify(self, scalar: float, muted: bool, actor: str) -> None:
        """音量通知（回调线程）：风暴去抖——每次通知重置静止计时器并计 burst。"""
        self._vol_burst += 1
        self._vol_last = (scalar, muted, actor)
        if not self._vol_hwnd:
            return
        if self._vol_timer_rid:
            _user32.KillTimer(self._vol_hwnd, VOL_TIMER_ID)
        rid = _user32.SetTimer(self._vol_hwnd, VOL_TIMER_ID, self._vol_settle_ms,
                               self._vol_timerproc_ref)
        self._vol_timer_rid = rid or VOL_TIMER_ID

    def _on_vol_timer(self, hwnd, msg, timer_id, dwtime) -> None:
        """静止判定达成：发布一次去抖后的音量调整事件（A2 的观察输入）。"""
        if self._vol_hwnd:
            _user32.KillTimer(self._vol_hwnd, VOL_TIMER_ID)
        self._vol_timer_rid = 0
        burst, last = self._vol_burst, self._vol_last
        self._vol_burst = 0
        self._vol_last = None
        if last is None:
            return
        scalar, muted, actor = last
        payload = {
            "device_id": self._vol_device_id, "scalar": round(float(scalar), 4),
            "muted": bool(muted), "actor": actor, "burst": burst,
            "knob": "master",
        }
        if self._vol_prev_scalar is not None:
            payload["from"] = round(float(self._vol_prev_scalar), 4)  # 归因幅度/方向的原料
        self._vol_prev_scalar = float(scalar)
        self._bus.publish_threadsafe(Event(SensorNS.VOLUME, "changed", payload))


def _device_meta(dev) -> dict:
    """尽力而为的设备元信息：任一步失败都不影响事件发布。"""
    meta = {}
    try:
        from comtypes import GUID
        from pycaw.pycaw import PROPERTYKEY

        store = dev.OpenPropertyStore(0)  # 0 = STGM_READ
        for key_spec, name, mapping in (
            (_PKEY_DEVICE_FRIENDLY_NAME, "name", None),
            (_PKEY_ENDPOINT_FORM_FACTOR, "form_factor", FORM_FACTORS),
        ):
            try:
                pk = PROPERTYKEY(GUID(key_spec[0]), key_spec[1])
                value = store.GetValue(pk)
                if value is None:
                    continue
                value = value.GetValue()
                if isinstance(value, str) and value:
                    meta[name] = value
                elif isinstance(value, int):
                    meta[name] = mapping.get(value, f"unknown_{value}") if mapping else value
            except Exception:
                continue
    except Exception:
        log.debug("读取设备属性失败", exc_info=True)
    return meta


def _make_notify_client(sink, on_default=None):
    """构造 IMMNotificationClient 回调实例（comtypes COMObject 模式）。

    注册给 COM 的是 QueryInterface 出来的接口指针（见 _run_sta），
    Python 对象本身由 self._client 强引用持有，防 GC 静默失效。
    on_default：默认设备变化钩子（pre-arm/音量回调迁移，回调线程内同步执行）。
    """
    from comtypes import COMObject
    from pycaw.pycaw import IMMNotificationClient

    class _NotifyClient(COMObject):
        _com_interfaces_ = [IMMNotificationClient]

        def OnDeviceStateChanged(self, this, pwstrDeviceId, dwNewState):
            sink("state_changed", {
                "device_id": pwstrDeviceId,
                "state": DEVICE_STATES.get(dwNewState, str(dwNewState)),
            })
            return 0  # S_OK

        def OnDeviceAdded(self, this, pwstrDeviceId):
            sink("device_added", {"device_id": pwstrDeviceId})
            return 0

        def OnDeviceRemoved(self, this, pwstrDeviceId):
            sink("device_removed", {"device_id": pwstrDeviceId})
            return 0

        def OnDefaultDeviceChanged(self, this, flow, role, pwstrDefaultDeviceId):
            sink("default_changed", {
                "flow": FLOWS.get(flow, str(flow)),
                "role": ROLES.get(role, str(role)),
                "device_id": pwstrDefaultDeviceId,
            })
            if on_default is not None:
                try:
                    on_default(flow, role, pwstrDefaultDeviceId)
                except Exception:
                    log.exception("默认设备变化钩子失败")
            return 0

        def OnPropertyValueChanged(self, this, pwstrDeviceId, key):
            # 属性值变更风暴，且音量走 IAudioEndpointVolumeCallback（更精准），
            # 此处保持丢弃。
            return 0

    return _NotifyClient()


def _make_volume_callback(sink):
    """构造 IAudioEndpointVolumeCallback：master 音量变化的精准通知。

    通知自带 context GUID——agent 的写操作带 AGENT_CONTEXT_GUID，
    用户调整（滑条/快捷键）为 NULL。actor 字段是 A2 归因"谁在动旋钮"
    的直接证据，无需猜测。
    """
    from comtypes import COMObject
    from pycaw.pycaw import IAudioEndpointVolumeCallback

    class _VolumeCallback(COMObject):
        _com_interfaces_ = [IAudioEndpointVolumeCallback]

        def OnNotify(self, this, pNotify):
            try:
                if pNotify:
                    data = pNotify.contents
                    ctx = str(data.guidEventContext)
                    actor = "agent" if ctx == str(AGENT_CONTEXT_GUID) else "user"
                    sink(data.fMasterVolume, data.bMuted, actor)
            except Exception:
                log.exception("音量通知处理失败")
            return 0

    return _VolumeCallback()
