"""B1 集成测试：Windows 信号优雅退出。

真实拉起 daemon 子进程（独立进程组）→ 发 CTRL_BREAK_EVENT →
验证优雅退出：stdout 有退出摘要，DB 里有完整 start/stop 事件对。
此前 bug：asyncio add_signal_handler 在 Windows 不可用，Ctrl+C 走
KeyboardInterrupt 裸退，stop 事件丢失（会话边界不完整）。
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest


def test_ctrl_break_graceful_stop(tmp_path):
    if os.name != "nt":
        pytest.skip("仅 Windows")

    db = tmp_path / "t.db"
    proc = subprocess.Popen(
        [sys.executable, "-m", "knowbot", "daemon", "--db", str(db), "--duration", "120"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        time.sleep(4)  # 等传感器就绪（COM 初始化 + 快照）
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        out, _ = proc.communicate(timeout=20)
    except Exception:
        proc.kill()
        raise

    text = out.decode("utf-8", "replace")
    assert "收到停止信号" in text          # 信号处理器真的被调用了
    assert '"dropped"' in text            # 优雅退出摘要被打印

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT type, payload FROM event_raw WHERE sensor='system.daemon' ORDER BY id"
    ).fetchall()
    conn.close()
    types = [t for t, _ in rows]
    assert "start" in types and "stop" in types   # 会话边界完整
    stop_payload = json.loads([p for t, p in rows if t == "stop"][0])
    assert stop_payload["flushed"] > 0            # 缓冲已冲刷落库
