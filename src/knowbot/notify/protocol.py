"""knowbot: URI 协议 —— toast 三键校准按钮的回传通道。

链路：toast 按钮(protocol 激活) → Windows Shell 查 HKCU\\Software\\Classes\\knowbot
→ `pythonw -m knowbot calibrate "<uri>" --db <绝对路径>` → 学习器落库。

注册幂等（每次 daemon 启动重写，venv/库路径迁移自愈），HKCU 无需管理员。
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

SCHEME = "knowbot"
CALIBRATE_VALUES = ("soft", "ok", "loud")


def calibrate_uri(value: str, pref_id: int, action_id: int | None = None) -> str:
    """构造三键校准按钮的回传 URI（技能侧唯一入口）。"""
    uri = f"{SCHEME}:calibrate?value={value}&pref={pref_id}"
    if action_id is not None:
        uri += f"&action={action_id}"
    return uri


def parse_calibrate_uri(uri: str) -> dict | None:
    """knowbot:calibrate?value=ok&pref=17&action=42 → {value, pref, action}。

    非法输入返回 None（CLI 侧据此拒绝执行）。
    """
    try:
        rest = uri.split(":", 1)[1]
        if rest.startswith("//"):
            rest = rest[2:]
        _, _, query = rest.partition("?")
        q = parse_qs(query)
        value = q["value"][0]
        pref = int(q["pref"][0])
        action = int(q["action"][0]) if q.get("action") else None
        if value not in CALIBRATE_VALUES or pref <= 0:
            return None
        return {"value": value, "pref": pref, "action": action}
    except Exception:
        return None


def ensure_protocol_handler(db_path: str) -> bool:
    """幂等注册 knowbot: 协议到 HKCU（仅 Windows；失败由调用方降级为无按钮）。"""
    if sys.platform != "win32":
        return False
    import winreg

    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")  # 无控制台窗口
    launcher = str(pythonw if pythonw.exists() else exe)
    db = str(Path(db_path).resolve())
    cmd = f'"{launcher}" -m knowbot calibrate "%1" --db "{db}"'

    root = rf"Software\Classes\{SCHEME}"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, root, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:knowbot calibration")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, root + r"\shell\open\command",
                            0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
    return True
