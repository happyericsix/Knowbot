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

    # 聚合层（A3）：爆发折叠为语义事件（audio.flow 命名空间）
    enable_aggregation: bool = True
    agg_settle_s: float = 2.0
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

    # Phase 1 ③：面板（localhost）与通知
    enable_panel: bool = True
    panel_port: int = 7681
    enable_toast: bool = True

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
