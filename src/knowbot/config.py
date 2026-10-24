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

    @classmethod
    def load(cls, path: str | None = None, overrides: dict | None = None) -> "Config":
        cfg = cls()
        names = {f.name for f in fields(cls)}
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
