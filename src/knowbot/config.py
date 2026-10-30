"""配置：全部阈值可配，不写死代码（方案 §4.3 的习惯从 Phase 0 养成）。

优先级：命令行覆盖 > config.toml [knowbot] 段 > 默认值。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10
    import tomli as tomllib


@dataclass
class Config:
    db_path: str = "./data/knowbot.db"
    heartbeat_period: float = 60.0
    queue_size: int = 10_000
    batch_size: int = 200
    flush_interval: float = 0.5
    enable_audio: bool = True
    enable_winmsg: bool = True

    # 空闲传感器（trickle 第一件）：GetLastInputInfo 轮询，只有数字无内容
    enable_idle: bool = True
    idle_threshold_s: float = 300.0   # 空闲越过阈值 → idle_begin（默认 5 分钟）
    idle_poll_s: float = 10.0         # 轮询周期（单次轻量 API 调用）

    # 聚合层（A3）：爆发折叠为语义事件（audio.flow 命名空间）
    enable_aggregation: bool = True
    # 折叠安静期：切换/插拔后安静这么久即定案。实测角色突发在 ~300ms 内
    # 完成，1.0s 已是安全余量（2026-10-27 由 2.0 压低：反馈延迟大头）
    agg_settle_s: float = 1.0
    agg_churn_flips: int = 3
    agg_max_span_s: float = 60.0
    agg_tick_interval: float = 0.5

    # Phase 1 ①：音量观察 + pre-arm（pre-arm 默认关，真机手动验证后再开）
    enable_volume_watch: bool = True
    vol_settle_s: float = 1.5
    enable_prearm: bool = False
    prearm_safe_scalar: float = 0.2
    prearm_only_lower: bool = True

    # Phase 1 ②：音量技能（首个 Skill，影子模式起步）
    enable_volume_skill: bool = True
    volume_skill_dry_run: bool = True

    # 计算礼貌（哲学 9）：影子决策 toast 节流——同设备冷却 + 全局日预算。
    # 决策照常落台账，只压制通知（实证：2026-10-27 23:10 风暴，75 秒 6 条）
    shadow_toast_cooldown_s: float = 600.0
    shadow_toast_daily: int = 30

    # prearm 冷却：同设备 N 秒内不重复布防（防快速来回切换的 see-saw 写放大）
    prearm_cooldown_s: float = 300.0

    # Phase 1 ③：面板（localhost）与通知
    enable_panel: bool = True
    panel_port: int = 7681
    enable_toast: bool = True

    # Phase 3：启动技能（解锁后开工组合，docs/phase3-startup-skill.md）
    enable_startup: bool = True
    startup_window_s: float = 900.0        # 节律观察窗（解锁后 N 秒内的启动集合）
    startup_min_samples: int = 5           # 最少解锁样本才生成组合草稿
    startup_member_rate_floor: float = 0.4 # 成员出现率淘汰线（衰减淘汰）
    startup_suggest_delay_s: float = 90.0  # 解锁后等 N 秒再开口（等你开首波）
    startup_silence_ratio: float = 0.6     # 手动开 ≥ 此比例 → 沉默记 missed_need

    # Phase 4：泛化提案管道（L1 实例→类，docs/phase4-generalization.md）
    enable_generalizer: bool = True
    generalizer_interval_s: float = 3600.0  # 扫描周期（纯 SQL，计算礼貌）

    # 中间地带传感器（修订 §21：逐传感器授权制，docs/authorized-sensors.md）
    # 未授权（面板未开）= 不启动采集；授权在面板"设置"区
    enable_webhost: bool = True
    webhost_browsers: list = None  # type: ignore[assignment]
    calendar_ics_url: str = ""     # 提供 URL 即授权；留空 = 不启用
    # 上下文类数据高时效（2026-10-30 架构分析）：忙闲是状态不是偏好，
    # 默认 30 分钟刷新；查询侧另有 TTL 保证（CalendarSensor.fresh_snapshot）
    calendar_refresh_hours: float = 0.5

    def __post_init__(self) -> None:
        if self.webhost_browsers is None:
            self.webhost_browsers = ["msedge.exe", "chrome.exe", "firefox.exe"]

    @classmethod
    def load(cls, path: str | None = None, overrides: dict | None = None) -> "Config":
        cfg = cls()
        names = {f.name for f in fields(cls)}
        if path is None and Path("config.toml").exists():
            path = "config.toml"  # 自动加载当前目录配置（daemon 与全部 CLI 命令一致）
        if path and Path(path).exists():
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
            section = data.get("knowbot", data)
            for key, value in section.items():
                if key in names:
                    setattr(cfg, key, value)
        if overrides:
            for key, value in overrides.items():
                if value is not None and key in names:
                    setattr(cfg, key, value)
        return cfg
