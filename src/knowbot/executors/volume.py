"""音量执行器：IAudioEndpointVolume 最小封装 + pre-arm 决策。

线程纪律：本模块不初始化 COM——调用线程必须已经 CoInitialize
（音频传感器 STA 线程天然满足；②的事件循环线程接入时再补懒初始化）。
"""
from __future__ import annotations

import comtypes
from comtypes import GUID
from pycaw.pycaw import IAudioEndpointVolume

# agent 写操作的身份标识：音量通知回调里凭它认出"这是我们自己设的"。
# 用户手动调整（滑条/快捷键/其他程序）到来的通知 context 为 NULL 或别的 GUID。
AGENT_CONTEXT_GUID = GUID("{7A5B9C31-4E2A-4F8B-9C6D-1F0E3A2B4C55}")

CLSCTX_ALL = comtypes.CLSCTX_ALL


def decide_prearm(prev_default: str | None, new_default: str | None,
                  current_scalar: float | None, safe_scalar: float,
                  only_lower: bool = True) -> float | None:
    """pre-arm 决策（纯函数，可测）：是否需要把刚休眠的端点预设到安全值。

    场景：默认输出从 prev 切到 new，prev 端点即将无人使用——
    趁它休眠把音量压到安全值，下次拔出耳机瞬间回到它时不会炸耳。
    方向不对称：默认只降不升（安全方向自动执行的红线）。
    """
    if not prev_default or not new_default or prev_default == new_default:
        return None
    if current_scalar is None:
        return None
    if only_lower and current_scalar <= safe_scalar:
        return None  # 已经够安全，不动（避免把用户调低的音量抬高）
    return safe_scalar


def endpoint_volume(enumerator, device_id: str | None = None,
                    flow: int = 0, role: int = 1) -> IAudioEndpointVolume:
    """取端点的音量控制接口。device_id 为空则取当前默认（render/multimedia）。"""
    if device_id:
        dev = enumerator.GetDevice(device_id)
    else:
        dev = enumerator.GetDefaultAudioEndpoint(flow, role)
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return iface.QueryInterface(IAudioEndpointVolume)


def get_master_scalar(enumerator, device_id: str | None = None) -> float | None:
    try:
        return float(endpoint_volume(enumerator, device_id).GetMasterVolumeLevelScalar())
    except Exception:
        return None


def set_master_scalar(enumerator, value: float,
                      device_id: str | None = None) -> float | None:
    """设置 master scalar，返回执行前的值（台账/undo 原料）。失败返回 None。"""
    try:
        vol = endpoint_volume(enumerator, device_id)
        before = float(vol.GetMasterVolumeLevelScalar())
        vol.SetMasterVolumeLevelScalar(float(value), AGENT_CONTEXT_GUID)
        return before
    except Exception:
        return None


class VolumeExecutor:
    """技能侧适配器：事件循环线程用，懒初始化 MTA COM 与枚举器。

    get(device_id) -> float | None；set(device_id, value) -> before | None。
    """

    def __init__(self) -> None:
        self._enum = None

    def _ensure(self):
        if self._enum is None:
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError:
                pass  # 线程已有 COM 上下文（同 Apartment 可用）
            from pycaw.constants import CLSID_MMDeviceEnumerator
            from pycaw.pycaw import IMMDeviceEnumerator
            self._enum = comtypes.CoCreateInstance(
                CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                comtypes.CLSCTX_INPROC_SERVER)
        return self._enum

    def get(self, device_id: str) -> float | None:
        try:
            return get_master_scalar(self._ensure(), device_id)
        except Exception:
            return None

    def set(self, device_id: str, value: float) -> float | None:
        try:
            return set_master_scalar(self._ensure(), value, device_id)
        except Exception:
            return None
