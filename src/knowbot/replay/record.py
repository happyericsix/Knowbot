"""导出 golden file：event_raw → JSONL（回放与回归测试的语料格式）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def dump_jsonl(events: Iterable[dict], out: str | Path | None) -> int:
    """out 为 None 时打印到 stdout；返回写出条数。"""
    n = 0
    if out is None:
        for ev in events:
            print(json.dumps(ev, ensure_ascii=False))
            n += 1
        return n
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            n += 1
    return n
