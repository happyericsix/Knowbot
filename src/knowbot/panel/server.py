"""面板服务：http.server（标准库）+ 每请求独立 SQLite 连接（WAL 多连接安全）。

GET 只读（mode=ro）；POST 写连接带 busy_timeout（与 daemon 写路径短事务互让）。
撤销走注入的执行器（daemon 传 VolumeExecutor，测试传 Fake）；
三键校准直接走学习器（与 toast 回传同一代码路径）。
"""
from __future__ import annotations

import datetime
import json
import logging
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..store.memory import now_iso

log = logging.getLogger(__name__)

_STATIC = Path(__file__).with_name("static") / "index.html"


def _connect(db_path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


class PanelServer:
    def __init__(self, db_path, executor=None, host: str = "127.0.0.1",
                 port: int = 7681, flags: dict | None = None) -> None:
        self.db_path = Path(db_path)
        self.executor = executor
        self.host, self.port = host, port
        self.flags = flags or {}
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        server = self

        class Handler(_Handler):
            panel = server

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="knowbot-panel", daemon=True)
        self._thread.start()
        log.info("面板: http://%s:%d", self.host, self._httpd.server_address[1])

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None

    @property
    def bound_port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self.port


class _Handler(BaseHTTPRequestHandler):
    panel: PanelServer

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ---------- 基础 ----------

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ---------- 路由 ----------

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                body = _STATIC.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/overview":
                return self._overview()
            if self.path == "/api/proposals":
                return self._proposals()
            if self.path.startswith("/api/preferences"):
                return self._preferences()
            if self.path.startswith("/api/actions"):
                return self._actions(self._limit())
            if self.path.startswith("/api/events"):
                return self._events(self._limit())
            self._json({"error": "not found"}, 404)
        except Exception as e:
            log.exception("面板 GET 失败")
            self._json({"error": repr(e)}, 500)

    def do_POST(self):
        try:
            if self.path.startswith("/api/preference/") and self.path.endswith("/confirm"):
                return self._pref_confirm(self._id_from_path("/confirm"))
            if self.path.startswith("/api/preference/") and self.path.endswith("/reject"):
                return self._pref_reject(self._id_from_path("/reject"))
            if self.path.startswith("/api/action/") and self.path.endswith("/undo"):
                return self._action_undo(self._id_from_path("/undo"))
            if self.path == "/api/pause":
                return self._pause(self._body())
            if self.path == "/api/calibrate":
                return self._calibrate(self._body())
            if self.path.startswith("/api/proposal/") and self.path.endswith("/accept"):
                return self._proposal_decide(self._id_from_path("/accept"), True)
            if self.path.startswith("/api/proposal/") and self.path.endswith("/reject"):
                return self._proposal_decide(self._id_from_path("/reject"), False)
            if self.path.startswith("/api/action/") and self.path.endswith("/skip"):
                return self._action_skip(self._id_from_path("/skip"))
            self._json({"error": "not found"}, 404)
        except sqlite3.OperationalError as e:
            self._json({"error": f"数据库忙: {e!r}"}, 503)
        except Exception as e:
            log.exception("面板 POST 失败")
            self._json({"error": repr(e)}, 500)

    def _limit(self) -> int:
        try:
            return min(500, int(self.path.split("limit=")[1]))
        except (IndexError, ValueError):
            return 100

    def _id_from_path(self, suffix: str) -> int:
        # /api/preference/<id>/confirm
        return int(self.path.split("/")[3])

    # ---------- GET 实现 ----------

    def _overview(self):
        with _connect(self.panel.db_path, readonly=True) as c:
            total = c.execute("SELECT COUNT(*) FROM event_raw").fetchone()[0]
            sensors = {r["sensor"]: r["c"] for r in c.execute(
                "SELECT sensor, COUNT(*) c FROM event_raw GROUP BY sensor")}
            last_ev, last_wall = c.execute(
                "SELECT sensor, ts_wall FROM event_raw ORDER BY id DESC LIMIT 1").fetchone() or ("", "")
            prefs = {r["state"]: r["c"] for r in c.execute(
                "SELECT state, COUNT(*) c FROM preference GROUP BY state")}
            actions = c.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
            by_react = {r["reaction"]: r["c"] for r in c.execute(
                "SELECT reaction, COUNT(*) c FROM action_log WHERE reaction IS NOT NULL"
                " GROUP BY reaction")}
            paused = (c.execute(
                "SELECT value FROM meta WHERE key='paused'").fetchone() or ["0"])[0]
            rows = c.execute(
                "SELECT ts_wall, quadrant, reaction, exec_mode, skill FROM action_log").fetchall()
        self._json({
            "events_total": total, "sensors": sensors, "last_event": last_wall,
            "preferences": prefs, "actions_total": actions,
            "reactions": by_react, "paused": paused == "1",
            "quadrant_week": _quadrant_week(rows),
            "flags": self.panel.flags,
        })

    def _preferences(self):
        with _connect(self.panel.db_path, readonly=True) as c:
            rows = [dict(r) | {"evidence_count": r["n_ev"]} for r in c.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM evidence e WHERE e.pref_id=p.id) n_ev"
                " FROM preference p ORDER BY p.state='draft' DESC, p.updated_at DESC")]
        self._json({"items": rows})

    def _actions(self, limit):
        with _connect(self.panel.db_path, readonly=True) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT a.*, p.scope_key AS pref_scope"
                " FROM action_log a LEFT JOIN preference p ON p.id=a.pref_id"
                " ORDER BY a.id DESC LIMIT ?", (limit,))]
        self._json({"items": rows})

    def _events(self, limit):
        with _connect(self.panel.db_path, readonly=True) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, ts_wall, sensor, type, payload FROM event_raw"
                " ORDER BY id DESC LIMIT ?", (limit,))]
        self._json({"items": rows})

    def _proposals(self):
        with _connect(self.panel.db_path, readonly=True) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM proposal ORDER BY status='pending' DESC, id DESC")]
        self._json({"items": rows})

    # ---------- POST 实现 ----------

    def _pref_confirm(self, pref_id: int):
        with _connect(self.panel.db_path, readonly=False) as c:
            c.execute("UPDATE preference SET state='active',"
                      " confidence=MAX(confidence, 0.9), source='explicit',"
                      " updated_at=? WHERE id=?", (now_iso(), pref_id))
            row = c.execute("SELECT * FROM preference WHERE id=?", (pref_id,)).fetchone()
        self._json({"ok": bool(row), "preference": dict(row) if row else None})

    def _pref_reject(self, pref_id: int):
        with _connect(self.panel.db_path, readonly=False) as c:
            row = c.execute("SELECT * FROM preference WHERE id=?", (pref_id,)).fetchone()
            if not row:
                return self._json({"ok": False}, 404)
            c.execute("INSERT INTO exception (pref_key, scope_key, reason, created_at)"
                      " VALUES (?,?,?,?)"
                      " ON CONFLICT(pref_key, scope_key) DO NOTHING",
                      (row["pref_key"], row["scope_key"], "user_explicit", now_iso()))
            c.execute("UPDATE preference SET state='archived', updated_at=? WHERE id=?",
                      (now_iso(), pref_id))
        self._json({"ok": True, "suppressed": True})

    def _action_undo(self, action_id: int):
        with _connect(self.panel.db_path, readonly=True) as c:
            row = c.execute("SELECT * FROM action_log WHERE id=?", (action_id,)).fetchone()
        if not row:
            return self._json({"ok": False, "error": "action 不存在"}, 404)
        action = json.loads(row["action"])
        if row["exec_mode"] == "dry_run":
            return self._json({"ok": False, "error": "影子动作无实际效果，无需撤销"})
        restore = (json.loads(row["undo_payload"]) or {}).get("restore") \
            if row["undo_payload"] else None
        device = action.get("device_id")
        if restore is None or device is None or not self.panel.executor:
            return self._json({"ok": False, "error": "缺少撤销载荷或执行器"})
        before = self.panel.executor.set(device, restore)
        with _connect(self.panel.db_path, readonly=False) as c:
            c.execute(
                "INSERT INTO action_log (ts_wall, ts_mono, skill, trigger_event_id,"
                " pref_id, action, exec_mode, risk, latency_ms)"
                " VALUES (?, 0, 'volume', NULL, ?, ?, 'silent', 'zero', NULL)",
                (now_iso(), row["pref_id"], json.dumps(
                    {"type": "undo", "ref_action": action_id,
                     "before": before, "after": restore}, ensure_ascii=False)))
            c.execute("UPDATE action_log SET reaction='reject',"
                      " quadrant='false_alarm', resolved_at=? WHERE id=?",
                      (now_iso(), action_id))
        self._json({"ok": True, "restored": restore})

    def _pause(self, body: dict):
        on = "1" if body.get("on") else "0"
        with _connect(self.panel.db_path, readonly=False) as c:
            c.execute("INSERT INTO meta (key, value) VALUES ('paused', ?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (on,))
        self._json({"ok": True, "paused": on == "1"})

    def _calibrate(self, body: dict):
        """三键校准（面板入口）：与 toast 回传同一学习器代码路径。"""
        from ..learning.learner import VolumeLearner
        from ..store.memory import MemoryStore

        value, pref_id = body.get("value"), body.get("pref_id")
        action_id = body.get("action_id")
        if value not in ("soft", "ok", "loud") or pref_id is None:
            return self._json({"ok": False, "error": "value/pref_id 缺失或非法"}, 400)
        try:
            with _connect(self.panel.db_path, readonly=False) as c:
                result = VolumeLearner(MemoryStore(c)).apply_calibration(
                    value, int(pref_id),
                    int(action_id) if action_id is not None else None, via="panel")
        except sqlite3.OperationalError as e:
            return self._json({"ok": False, "error": f"数据库忙: {e!r}"}, 503)
        self._json(result, 200 if result.get("ok") else 409)

    def _proposal_decide(self, proposal_id: int, accept: bool):
        """泛化提案裁决：接受=类信念升格；拒绝=例外表永久抑制。"""
        from ..learning.generalize import apply_proposal
        from ..store.memory import MemoryStore

        try:
            with _connect(self.panel.db_path, readonly=False) as c:
                result = apply_proposal(MemoryStore(c), proposal_id, accept,
                                        via="panel")
        except sqlite3.OperationalError as e:
            return self._json({"ok": False, "error": f"数据库忙: {e!r}"}, 503)
        self._json(result, 200 if result.get("ok") else 409)

    def _action_skip(self, action_id: int):
        """影子期遗留台账的人工收尾：记 ignore（不进四象限，退出待裁决）。"""
        with _connect(self.panel.db_path, readonly=False) as c:
            row = c.execute("SELECT id FROM action_log WHERE id=? AND reaction"
                            " IS NULL", (action_id,)).fetchone()
            if not row:
                return self._json({"ok": False, "error": "不存在或已裁决"}, 404)
            c.execute("UPDATE action_log SET reaction='ignore',"
                      " reaction_detail=?, resolved_at=? WHERE id=?",
                      (json.dumps({"via": "manual_skip"}, ensure_ascii=False),
                       now_iso(), action_id))
        self._json({"ok": True})


def _parse_wall(ts: str) -> datetime.datetime | None:
    """兼容两种历史格式：ISO（含时区）与 sqlite datetime()（本地、无时区）。"""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.astimezone()
    except ValueError:
        return None


def _quadrant_week(rows, window_days: int = 7,
                   health_line: float = 0.10) -> dict:
    """周窗四象限统计：主动性的仪表盘（哲学 10，周 FA 率 < 健康线）。"""
    cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(days=window_days)
    counts = {"correct_detection": 0, "false_alarm": 0,
              "missed_need": 0, "non_response": 0}
    unresolved = 0
    for r in rows:
        quadrant = r["quadrant"]
        if quadrant:
            ts = _parse_wall(r["ts_wall"])
            if ts is not None and ts >= cutoff:
                counts[quadrant] = counts.get(quadrant, 0) + 1
        elif (r["reaction"] is None and r["exec_mode"] != "none"
              and r["skill"] != "prearm"):
            unresolved += 1  # 出过手（含影子）但还没裁决的；prearm 不可见布防
            # 无观察窗、不期待裁决，不计入（其失败信号 = 面板撤销）
    fired = counts["false_alarm"] + counts["correct_detection"]
    fa_rate = (counts["false_alarm"] / fired) if fired else None
    return {"window_days": window_days, "counts": counts,
            "fa_rate": fa_rate, "health_line": health_line,
            "unresolved": unresolved}
