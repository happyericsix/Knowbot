"""中间地带授权制（修订 §21，docs/authorized-sensors.md）。

- 未授权 = 不启动采集（无追溯空白是设计结果）；
- 授权/撤销是治理事件（system.auth 落 event_raw 留痕）；
- 撤销即焚：停止采集 + 删除该传感器全部历史事件 + 焚毁标记留痕；
- 授权只能来自用户在面板的直接操作，任何通知/建议不得诱导。
"""
from __future__ import annotations

AUTH_PREFIX = "auth."          # meta 键前缀：auth.<sensor> = 授予时刻 ISO
REVOKED_PREFIX = "auth.revoked."  # 撤销标记（阻止日历启动自动重授权）

# 中间地带传感器注册表：面板设置区的人话说明与合法性边界
MIDDLE_TIER: dict[str, dict] = {
    "app.web": {
        "name": "浏览器域名",
        "desc": "前台浏览器当前网站的域名（仅 host，如 douban.com）。"
                "地址与搜索栏内容在内存中即时截断，完整 URL、路径、"
                "搜索词永不落库、永不进日志；隐私窗口自动跳过。",
    },
    "calendar.busy": {
        "name": "日历忙闲",
        "desc": "未来 7 天的忙时段起止时间（只取开始/结束，标题、"
                "参与人、地点在解析时即丢弃）。需在 config.toml 提供 "
                "ICS 订阅 URL（提供 URL 即授权）。",
    },
}


def is_authorized(memory, sensor_name: str) -> bool:
    """授权探针（daemon 侧注入传感器）。memory 为 MemoryStore。"""
    return (memory.get_meta(f"{AUTH_PREFIX}{sensor_name}") != ""
            and memory.get_meta(f"{REVOKED_PREFIX}{sensor_name}") != "1")


def grant(memory, sensor_name: str) -> None:
    from ..store.memory import now_iso
    memory.set_meta(f"{REVOKED_PREFIX}{sensor_name}", "0")  # 清除撤销标记
    memory.set_meta(f"{AUTH_PREFIX}{sensor_name}", now_iso())


def revoke(memory, sensor_name: str) -> int:
    """撤销即焚：删授权键 + 焚毁标记 + 删除该传感器全部历史事件。
    返回焚毁的事件条数（删除前计数）。"""
    from ..store.memory import now_iso
    n = memory.conn.execute(
        "SELECT COUNT(*) FROM event_raw WHERE sensor=?",
        (sensor_name,)).fetchone()[0]
    memory.conn.execute("DELETE FROM event_raw WHERE sensor=?", (sensor_name,))
    memory.conn.commit()
    memory.set_meta(f"{REVOKED_PREFIX}{sensor_name}", "1")
    memory.set_meta(f"{AUTH_PREFIX}{sensor_name}", "")
    memory.set_meta(f"auth.purged.{sensor_name}", now_iso())
    return n
