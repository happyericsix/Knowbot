"""泛化提案管道测试：L1 候选/折扣/去重/抑制 + 面板裁决闭环。"""
import json

from knowbot.envelope import Event
from knowbot.learning.generalize import (Generalizer, apply_proposal,
                                         find_l1_candidates,
                                         generate_proposals)
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore, seed_defaults

DEV_A = "{0.0.0.00000000}.{aaaaaaaa-0000-0000-0000-000000000001}"
DEV_B = "{0.0.0.00000000}.{bbbbbbbb-0000-0000-0000-000000000002}"
CLS = "audio.form_factor:speakers"


def _setup(tmp_path):
    store = EventStore(tmp_path / "g.db", batch_size=10_000, flush_interval=0.05)
    memory = MemoryStore(store._conn)
    seed_defaults(memory)
    # 事实流：两台设备的 form_factor 证据（generalizer 靠它反查类归属）
    for dev in (DEV_A, DEV_B):
        store.append(Event("audio.flow", "device_settled",
                           {"device_id": dev, "form_factor": "speakers",
                            "final_state": "active", "flips": 1, "churn": False}))
    store._flush()
    return store, memory


def _two_instances(memory, scalar_a=0.25, scalar_b=0.25, source="explicit"):
    memory.get_or_create("audio.volume", "instance", f"audio.device:{DEV_A}",
                         {"scalar": scalar_a}, 0.9, source)
    memory.get_or_create("audio.volume", "instance", f"audio.device:{DEV_B}",
                         {"scalar": scalar_b}, 0.92, source)


def test_l1_candidate_needs_two_consistent_explicit(tmp_path):
    """门槛：单实例/低置信/值分歧/非 explicit 都不生成候选。"""
    store, memory = _setup(tmp_path)
    assert find_l1_candidates(memory) == []            # 零实例
    memory.get_or_create("audio.volume", "instance", f"audio.device:{DEV_A}",
                         {"scalar": 0.25}, 0.9, "explicit")
    assert find_l1_candidates(memory) == []            # 单实例
    memory.get_or_create("audio.volume", "instance", f"audio.device:{DEV_B}",
                         {"scalar": 0.5}, 0.9, "explicit")
    assert find_l1_candidates(memory) == []            # 值分歧（极差 0.25）
    store.close()

    store, memory = _setup(tmp_path)
    memory.get_or_create("audio.volume", "instance", f"audio.device:{DEV_A}",
                         {"scalar": 0.25}, 0.9, "explicit")
    memory.get_or_create("audio.volume", "instance", f"audio.device:{DEV_B}",
                         {"scalar": 0.25}, 0.5, "explicit")
    assert find_l1_candidates(memory) == []            # 逐条置信不足
    store.close()

    store, memory = _setup(tmp_path)
    _two_instances(memory, source="observed")
    assert find_l1_candidates(memory) == []            # 非用户确认不泛化
    store.close()


def test_l1_proposal_generate_dedup_and_apply(tmp_path):
    """生成→去重→接受：类信念升格 inferred + 折扣封顶。"""
    store, memory = _setup(tmp_path)
    _two_instances(memory)
    assert generate_proposals(memory) == 1
    assert generate_proposals(memory) == 0             # pending 去重

    row = memory.conn.execute(
        "SELECT * FROM proposal WHERE status='pending'").fetchone()
    content = json.loads(row["content"])
    assert content["scope_key"] == CLS and content["value"] == {"scalar": 0.25}
    assert abs(row["confidence"] - 0.9 * 0.85) < 1e-6  # min(0.9,0.92)×折扣

    r = apply_proposal(memory, row["id"], accept=True)
    assert r["ok"] and r["status"] == "accepted"
    cls = memory.get("audio.volume", "class", CLS)
    assert json.loads(cls["value"]) == {"scalar": 0.25}
    assert cls["source"] == "inferred"                  # 推断可被纠正降级
    assert abs(cls["confidence"] - 0.765) < 1e-6        # 0.9×0.85 封顶于来源
    # 再次扫描：值已一致但无新 pending（已接受），不再打扰
    assert generate_proposals(memory) == 0
    store.close()


def test_l1_reject_suppresses_forever(tmp_path):
    """拒绝 → exception 永久抑制：同类提案不再生成。"""
    store, memory = _setup(tmp_path)
    _two_instances(memory)
    generate_proposals(memory)
    pid = memory.conn.execute(
        "SELECT id FROM proposal WHERE status='pending'").fetchone()["id"]
    r = apply_proposal(memory, pid, accept=False)
    assert r["ok"] and r["suppressed"]
    assert memory.conn.execute(
        "SELECT 1 FROM exception WHERE scope_key=?", (CLS,)).fetchone()
    assert generate_proposals(memory) == 0              # 抑制生效
    # 二次裁决拒绝（已裁决）
    assert apply_proposal(memory, pid, accept=True)["ok"] is False
    store.close()


def test_generalizer_service_smoke(tmp_path):
    """daemon 服务形态：构造即扫（启动首拍）。"""
    import asyncio
    store, memory = _setup(tmp_path)
    _two_instances(memory)

    async def main():
        g = Generalizer(memory, interval_s=3600.0)
        g.start(asyncio.get_running_loop())
        await asyncio.sleep(0.1)
        g.stop()
    asyncio.run(main())
    n = memory.conn.execute("SELECT COUNT(*) FROM proposal").fetchone()[0]
    assert n == 1
    store.close()
