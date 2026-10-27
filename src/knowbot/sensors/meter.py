"""电平采样器：归因窗口内的受控采样（"触发全事件驱动，取证允许受控采样"）。

独立线程 + MTA COM，窗口结束即停——不做常驻采样。
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

SAMPLE_INTERVAL = 0.5   # 2Hz
ACTIVE_THRESHOLD = 0.05  # peak 超过此值视为有内容在放


class MeterSampler:
    """start() 后台采样；stop() 返回 MeterStats 或 None（设备不可用）。"""

    def __init__(self, enumerator_getter, device_id: str | None) -> None:
        self._get_enum = enumerator_getter   # () -> IMMDeviceEnumerator（懒加载）
        self._device_id = device_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peaks: list[float] = []
        self.error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="knowbot-meter",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self.error or not self.peaks:
            return None
        active = sum(1 for p in self.peaks if p > ACTIVE_THRESHOLD)
        return {
            "mean": sum(self.peaks) / len(self.peaks),
            "peak": max(self.peaks),
            "active_frac": active / len(self.peaks),
        }

    def _run(self) -> None:
        try:
            import comtypes
            from pycaw.pycaw import IAudioMeterInformation
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError:
                pass  # 线程已有 COM 上下文
            from pycaw.constants import CLSID_MMDeviceEnumerator
            from pycaw.pycaw import IMMDeviceEnumerator
            enum = self._get_enum() or comtypes.CoCreateInstance(
                CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                comtypes.CLSCTX_INPROC_SERVER)
            from ..executors.volume import endpoint_volume
            dev = (enum.GetDevice(self._device_id) if self._device_id
                   else enum.GetDefaultAudioEndpoint(0, 1))
            iface = dev.Activate(IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None)
            meter = iface.QueryInterface(IAudioMeterInformation)
            while not self._stop.is_set():
                self.peaks.append(float(meter.GetPeakValue()))
                time.sleep(SAMPLE_INTERVAL)
        except Exception as e:
            self.error = repr(e)
            log.debug("电平采样不可用：%r", e)
