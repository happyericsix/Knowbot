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


def _cmd_calibrate(args) -> None:
    """toast 校准按钮的回传落点：knowbot:calibrate?... → 学习器（pythonw 静默运行）。"""
    import sqlite3
    import time
    from pathlib import Path

    from .learning.learner import VolumeLearner
    from .notify.protocol import parse_calibrate_uri
    from .store.memory import MemoryStore

    params = parse_calibrate_uri(args.uri)
    if params is None:
        print(f"非法校准 URI: {args.uri}", file=sys.stderr)
        raise SystemExit(2)
    db = args.db or Config.load(args.config).db_path
    result = {"ok": False, "error": "unreachable"}
    try:
        conn = sqlite3.connect(db, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            result = VolumeLearner(MemoryStore(conn)).apply_calibration(
                params["value"], params["pref"], params["action"], via="toast")
        finally:
            conn.close()
    except Exception as e:
        result = {"ok": False, "error": repr(e)}
    # pythonw 无控制台：结果留痕到 data/calibrate.log（诊断回传链路用）
    try:
        log_path = Path(db).parent / "calibrate.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {args.uri} -> "
                    f"{json.dumps(result, ensure_ascii=False)}\n")
    except Exception:
        pass
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("ok") else 1)


def _cmd_action(args) -> None:
    """toast 动作按钮的回传落点（启动技能：拉起/不用/以后自动）。"""
    import sqlite3
    import time
    from pathlib import Path

    from .notify.protocol import parse_action_uri
    from .skills.startup import apply_startup_action
    from .store.memory import MemoryStore

    params = parse_action_uri(args.uri)
    if params is None or params.get("skill", "startup") != "startup":
        print(f"非法动作 URI: {args.uri}", file=sys.stderr)
        raise SystemExit(2)
    db = args.db or Config.load(args.config).db_path
    rehearsal = Config.load(args.config).rehearsal_mode
    result = {"ok": False, "error": "unreachable"}
    try:
        conn = sqlite3.connect(db, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            if rehearsal:  # 演练模式：回传按钮也零副作用
                from .executors.launch import FakeLaunchExecutor
                launcher = FakeLaunchExecutor()
            else:
                from .executors.launch import LaunchExecutor
                launcher = LaunchExecutor()
            result = apply_startup_action(
                MemoryStore(conn), launcher, params["op"],
                params["pref"], params["action"], via="toast")
        finally:
            conn.close()
    except Exception as e:
        result = {"ok": False, "error": repr(e)}
    try:  # pythonw 无控制台：留痕供诊断
        with open(Path(db).parent / "action.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {args.uri} -> "
                    f"{json.dumps(result, ensure_ascii=False)}\n")
    except Exception:
        pass
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("ok") else 1)


def _cmd_install_protocol(args) -> None:
    from .notify.protocol import ensure_protocol_handler
    from .notify.toast import ensure_appid

    ensure_appid()  # 未注册 AUMID 的 toast 静默不显示
    db = args.db or Config.load(args.config).db_path
    try:
        ok = ensure_protocol_handler(db)
    except Exception as e:
        print(f"注册失败: {e!r}", file=sys.stderr)
        raise SystemExit(1)
    print("knowbot: 协议与 AUMID 已注册" if ok else "非 Windows 平台，跳过注册")


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

    p = sub.add_parser("calibrate", help="toast 校准回传落点（knowbot: URI → 记忆库）")
    p.add_argument("uri", help="knowbot:calibrate?value=ok&pref=17&action=42")
    p.add_argument("--config", default=None)
    p.add_argument("--db", default=None, help="覆盖数据库路径（协议注册时写入）")
    p.set_defaults(func=_cmd_calibrate)

    p = sub.add_parser("action", help="toast 动作按钮回传（启动技能拉起/不用/自动）")
    p.add_argument("uri", help="knowbot:action?skill=startup&op=launch&pref=5")
    p.add_argument("--config", default=None)
    p.add_argument("--db", default=None)
    p.set_defaults(func=_cmd_action)

    p = sub.add_parser("install-protocol", help="注册 knowbot: URI 协议（toast 按钮回传）")
    p.add_argument("--config", default=None)
    p.add_argument("--db", default=None)
    p.set_defaults(func=_cmd_install_protocol)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    args.func(args)
