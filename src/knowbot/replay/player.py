"""回放器：golden file → handler。

Phase 0 职责是校验：保序、mono 时轴单调、会话切分。
Phase 1 起喂给技能测试（事件流 × 记忆初态 → 期望动作序列）。

会话边界：system.clock_reconcile（睡眠唤醒，mono 回绕）与
system.daemon/stop（进程重启，seq/mono 均回绕）。边界后第一事件
开启新会话，跨边界的时轴回绕不计违规。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Iterable


def is_session_boundary(ev: dict) -> bool:
    return (
        ev.get("sensor") == "system.clock_reconcile"
        or (ev.get("sensor") == "system.daemon" and ev.get("type") == "stop")
    )


def play(events: Iterable[dict], handler: Callable[[dict], None], speed: float = 0.0) -> dict:
    """按录制顺序回放。speed=0 全速；speed>0 按事件时间差/speed 实时回放。"""
    stats = {"events": 0, "sessions": 0, "mono_violations": 0, "seq_violations": 0}
    last_mono = None
    last_seq = None
    prev_mono = None

    for ev in events:
        if is_session_boundary(ev):
            last_mono = last_seq = prev_mono = None
        else:
            if last_mono is not None:
                if ev["ts_mono"] < last_mono:
                    stats["mono_violations"] += 1
                if ev["seq"] <= last_seq:
                    stats["seq_violations"] += 1
            elif stats["events"]:
                stats["sessions"] += 1  # 边界后第一事件：开新会话
            last_mono = ev["ts_mono"]
            last_seq = ev["seq"]

        if speed > 0 and prev_mono is not None:
            delay = max(0.0, ev["ts_mono"] - prev_mono) / speed
            if delay > 0:
                time.sleep(delay)
        prev_mono = ev["ts_mono"]

        handler(ev)
        stats["events"] += 1

    if stats["events"]:
        stats["sessions"] = max(1, stats["sessions"] + 1)
    return stats


def load_file(path: str | Path) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
