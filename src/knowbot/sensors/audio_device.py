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
from ctypes import wintypes

from ..bus import EventBus
from ..envelope import Event
from .base import Sensor

log = logging.getLogger(__name__)

WM_QUIT = 0x0012
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"

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

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._thread: threading.Thread | None = None
        self._client = None    # 防 GC：COMObject 回调对象强引用
        self._ptr = None       # 注册用的接口指针，防 GC
        self._enumerator = None  # 防 GC：枚举器强引用

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
            self._client = _make_notify_client(self._on_com_event)
            self._ptr = self._client.QueryInterface(IMMNotificationClient)
            self._enumerator.RegisterEndpointNotificationCallback(self._ptr)
            self._snapshot()
            self._pump_messages()
        except Exception as e:
            log.exception("音频传感器运行失败")
            self._report_unavailable(repr(e))
        finally:
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
        # 返回 0 = WM_QUIT（正常退出）；-1 = 错误
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass

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


def _make_notify_client(sink):
    """构造 IMMNotificationClient 回调实例（comtypes COMObject 模式）。

    注册给 COM 的是 QueryInterface 出来的接口指针（见 _run_sta），
    Python 对象本身由 self._client 强引用持有，防 GC 静默失效。
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
            return 0

        def OnPropertyValueChanged(self, this, pwstrDeviceId, key):
            # 属性值变更风暴（拖音量滑条即高频触发），Phase 0 不采集
            return 0

    return _NotifyClient()
