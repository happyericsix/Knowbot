"""knowbot 命令行：daemon / tail / stats / dump / replay。"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Config


def _cmd_daemon(args) -> None:
    import asyncio

    from .daemon import run

    overrides = {"db_path": args.db} if args.db else None
    config = Config.load(args.config, overrides=overrides)
    try:
        summary = asyncio.run(run(config, duration=args.duration))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return
    print(json.dumps(summary, ensure_ascii=False))


def _cmd_tail(args) -> None:
    from .store.db import tail_events

    db = args.db or Config.load(args.config).db_path
    try:
        events = tail_events(db, args.num)
    except Exception as e:
        print(f"读取失败（数据库不存在或未初始化？{db}）：{e}", file=sys.stderr)
        raise SystemExit(1)
    if not events:
        print(f"（无事件）{db}")
        return
    print(f"{'id':>7}  {'ts_wall':<29} {'sensor':<22} {'type':<14} payload")
    for ev in events:  # 新事件在前
        payload = json.dumps(ev["payload"], ensure_ascii=False)
        if len(payload) > 70:
            payload = payload[:67] + "..."
        print(f"{ev['id']:>7}  {ev['ts_wall']:<29} {ev['sensor']:<22} {ev['type']:<14} {payload}")


def _cmd_stats(args) -> None:
    from .store.db import sensor_stats

    db = args.db or Config.load(args.config).db_path
    stats = sensor_stats(db)
    print(f"总计 {stats['total']} 条事件 · schema v{stats['user_version']}")
    for sensor, info in stats["sensors"].items():
        print(f"  {sensor:<22} {info['count']:>7}  {info['first']} ~ {info['last']}")


def _cmd_dump(args) -> None:
    from .replay.record import dump_jsonl
    from .store.db import iter_events

    db = args.db or Config.load(args.config).db_path
    n = dump_jsonl(iter_events(db, args.wall_from, args.wall_to), args.out)
    target = args.out or "stdout"
    print(f"导出 {n} 条 → {target}", file=sys.stderr)


def _cmd_replay(args) -> None:
    from .replay.player import load_file, play

    events = load_file(args.file)
    stats = play(events, handler=lambda ev: None, speed=args.speed)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="knowbot", description="个人驻留事件 Agent —— Phase 0 事件记录仪")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("daemon", help="常驻记录（Ctrl+C 优雅退出）")
    p.add_argument("--config", default=None, help="config.toml 路径")
    p.add_argument("--db", default=None, help="覆盖数据库路径")
    p.add_argument("--duration", type=float, default=0.0,
                   help="运行秒数（0=常驻；冒烟测试用）")
    p.set_defaults(func=_cmd_daemon)

    p = sub.add_parser("tail", help="最近 N 条事件")
    p.add_argument("--config", default=None)
    p.add_argument("--db", default=None)
    p.add_argument("-n", "--num", type=int, default=20)
    p.set_defaults(func=_cmd_tail)

    p = sub.add_parser("stats", help="每传感器计数与时间范围")
    p.add_argument("--config", default=None)
    p.add_argument("--db", default=None)
    p.set_defaults(func=_cmd_stats)

    p = sub.add_parser("dump", help="导出 JSONL golden file")
    p.add_argument("--config", default=None)
    p.add_argument("--db", default=None)
    p.add_argument("--from", dest="wall_from", default=None,
                   help="起始 ts_wall（含），ISO 前缀即可，如 2026-10-24T18")
    p.add_argument("--to", dest="wall_to", default=None, help="结束 ts_wall（含）")
    p.add_argument("--out", default=None, help="输出文件路径，缺省打印 stdout")
    p.set_defaults(func=_cmd_dump)

    p = sub.add_parser("replay", help="回放 golden file（保序/单调/会话校验）")
    p.add_argument("--file", required=True)
    p.add_argument("--speed", type=float, default=0.0,
                   help="0=全速校验；>0 按事件时间差/该倍速回放")
    p.set_defaults(func=_cmd_replay)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    args.func(args)
