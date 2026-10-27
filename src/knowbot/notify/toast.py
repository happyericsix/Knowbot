"""toast 实现：PowerShell 调 WinRT，XML 经 base64 传递避免引号地狱。"""
from __future__ import annotations

import base64
import logging
import subprocess
import threading
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

_PS_TEMPLATE = """
$b = [Convert]::FromBase64String('{}')
$xml = [Text.Encoding]::UTF8.GetString($b)
try {{
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
  $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
  $doc.LoadXml($xml)
  $t = New-Object Windows.UI.Notifications.ToastNotification $doc
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{}').Show($t)
}} catch {{
  Write-Output $_.Exception.Message
  exit 1
}}
"""


# 点 toast 本体 = 打开面板（管理审计入口）；点底部按钮 = 瞬时决策（校准回传）。
# 端口与 config.panel_port 默认值一致；面板端口改动时此处随之调整。
_PANEL_URL = "http://127.0.0.1:7681/"


def _build_xml(title: str, body: str, actions: list | None = None) -> str:
    t = ET.Element("toast", {"launch": _PANEL_URL, "activationType": "protocol"})
    vis = ET.SubElement(t, "visual")
    bind = ET.SubElement(vis, "binding", {"template": "ToastGeneric"})
    ET.SubElement(bind, "text").text = title
    ET.SubElement(bind, "text").text = body
    if actions:
        # 三键校准等瞬时决策按钮：protocol 激活 → knowbot: URI → CLI 落库
        acts = ET.SubElement(t, "actions")
        for label, uri in actions:
            ET.SubElement(acts, "action", {
                "content": label, "arguments": uri,
                "activationType": "protocol"})
    return ET.tostring(t, encoding="unicode")


def ensure_appid(appid: str = "knowbot", display: str = "knowbot") -> bool:
    """注册 AUMID（BurntToast New-BTAppId 同款注册表做法）。

    未注册的 AUMID 调 Show() 不抛错但通知静默不显示（2026-10-27 真机实测：
    exit 0 且无任何 toast）。HKCU 注册表项幂等，仅 Windows。
    """
    import sys
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                rf"Software\Classes\AppUserModelId\{appid}", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, display)
        return True
    except OSError:
        log.warning("AUMID 注册失败（toast 可能不显示）", exc_info=True)
        return False


def _try_appid(appid: str, xml_b64: str) -> bool:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS_TEMPLATE.format(xml_b64, appid)],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def toast(title: str, body: str, actions: list | None = None) -> None:
    """异步弹 toast：先试 knowbot AppId，退回 PowerShell 官方 Id，再退回日志。

    actions：[(按钮文字, knowbot: URI)]，protocol 激活回传（三键校准）。
    """
    xml_b64 = base64.b64encode(
        _build_xml(title, body, actions).encode("utf-8")).decode()
    threading.Thread(
        target=lambda: (
            (_try_appid("knowbot", xml_b64)
             or _try_appid(
                 "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe",
                 xml_b64))
            or log.info("[toast 降级日志] %s | %s", title, body)
        ), daemon=True,
    ).start()
