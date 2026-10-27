"""A3 聚合层测试：合成黄金用例 + 真实语料回归。

真实语料（fixtures/audio_monitor_wake.jsonl）来自 2026-10-25 晨
显示器唤醒的 11 条 state_changed 爆发——线上形态永久进回归。
"""
import json
from pathlib import Path

from knowbot.aggregation.audio import AudioDeviceFolder, AudioFoldParams
from knowbot.envelope import Event


def ev(typ: str, payload: dict, ts: float) -> Event:
    return Event("audio.device", typ, payload, ts_mono=ts, ts_wall=f"w{ts}")


def test_single_flip_settles_after_quiet():
    """单次状态变化：安静 settle_s 后定案为一条 device_settled。"""
    f = AudioDeviceFolder(AudioFoldParams(settle_s=2.0))
    out = f.feed(ev("state_changed", {"device_id": "A", "state": "active", "name": "X"}, 10.0), 10.0)
    assert out == []
    out = f.tick(12.5)
    assert len(out) == 1
    e = out[0]
    assert e.sensor == "audio.flow" and e.type == "device_settled"
    p = e.payload
    assert p["final_state"] == "active" and p["flips"] == 1
    assert p["churn"] is False and p["derived"] is True and p["name"] == "X"


def test_monitor_burst_folds_to_one_churned_event():
    """爆发中不定案；安静后一条定案，flips=churn 标记。"""
    f = AudioDeviceFolder()
    out = []
    ts = 100.0
    states = ["not_present", "unplugged", "active", "not_present", "unplugged", "active"]
    for i, state in enumerate(states):
        out += f.feed(ev("state_changed", {"device_id": "NV", "state": state},
                         ts + i * 0.13), ts + i * 0.13)
    assert out == []  # 爆发进行中：不定案
    out += f.tick(ts + 3.0)  # 最后事件在 ts+0.65，安静 2.35s > settle
    assert len(out) == 1
    p = out[0].payload
    assert p["flips"] == 6 and p["churn"] is True
    assert p["final_state"] == "active"
    assert 0 < p["span_ms"] < 2000


def test_two_bursts_separated_by_gap_yield_two_events():
    """两段爆发被安静隔开 → 两条独立定案（对应真实语料形态）。"""
    f = AudioDeviceFolder(AudioFoldParams(settle_s=2.0))
    out = []
    out += f.feed(ev("state_changed", {"device_id": "NV", "state": "unplugged"}, 100.0), 100.0)
    out += f.tick(101.5)                       # 未到 settle：仍不定案
    assert out == []
    out += f.tick(102.5)                       # 定案第一条
    assert len(out) == 1
    out += f.feed(ev("state_changed", {"device_id": "NV", "state": "active"}, 190.0), 190.0)
    out += f.tick(192.5)                       # 定案第二条
    assert len(out) == 2
    assert [e.payload["final_state"] for e in out] == ["unplugged", "active"]


def test_device_removed_settles_immediately():
    """device_removed 是定案事件：窗口内到达立即出结果，不等 settle。

    （对应耳机拔出：unplugged 抖动后紧跟 device_removed。）
    """
    f = AudioDeviceFolder()
    out = f.feed(ev("state_changed", {"device_id": "A", "state": "unplugged"}, 10.0), 10.0)
    out += f.feed(ev("device_removed", {"device_id": "A"}, 10.5), 10.5)
    assert len(out) == 1 and out[0].payload["final_state"] == "removed"
    assert out[0].payload["flips"] == 2


def test_device_added_then_active_counts_once():
    """device_added + state_changed(active) 同窗口：合并为一条，final=active。"""
    f = AudioDeviceFolder()
    out = []
    out += f.feed(ev("device_added", {"device_id": "B"}, 10.0), 10.0)
    out += f.feed(ev("state_changed", {"device_id": "B", "state": "active"}, 10.3), 10.3)
    out += f.tick(12.5)
    assert len(out) == 1
    p = out[0].payload
    assert p["final_state"] == "active" and p["flips"] == 2 and p["churn"] is False


def test_default_changed_repeats_fold():
    """同角色同设备的连续 default_changed 折叠为一条 default_output（repeats）。"""
    f = AudioDeviceFolder()
    out = []
    for i in range(3):
        out += f.feed(ev("default_changed",
                         {"device_id": "A", "flow": "render", "role": "multimedia"},
                         10.0 + i * 0.2), 10.0 + i * 0.2)
    assert out == []
    out += f.tick(12.5)
    assert len(out) == 1 and out[0].type == "default_output"
    assert out[0].payload["repeats"] == 3


def test_default_changed_device_switch_emits_previous():
    """同角色换设备：旧设备立即定案，新设备开新窗口。"""
    f = AudioDeviceFolder()
    out = []
    out += f.feed(ev("default_changed", {"device_id": "A", "flow": "render", "role": "multimedia"}, 10.0), 10.0)
    out += f.feed(ev("default_changed", {"device_id": "B", "flow": "render", "role": "multimedia"}, 11.0), 11.0)
    assert len(out) == 1 and out[0].payload["device_id"] == "A"  # A 立即定案
    out += f.tick(13.5)
    assert len(out) == 2 and out[1].payload["device_id"] == "B"


def test_max_span_safety_valve():
    """安全阀：事件持续不断（间隔<settle）永无安静 → 靠 max_span 强制定案。"""
    f = AudioDeviceFolder(AudioFoldParams(settle_s=2.0, max_span_s=10.0))
    out = []
    ts = 100.0
    flips = 0
    while ts < 112.0:
        out += f.feed(ev("state_changed", {"device_id": "A", "state": "active"}, ts), ts)
        flips += 1
        ts += 1.5  # 间隔 1.5s < settle：窗口永远等不到安静
        if out:
            break
    assert len(out) == 1                      # 跨过 max_span 时被强制定案
    assert out[0].payload["flips"] == flips   # 定案包含全部翻转
    assert out[0].payload["span_ms"] >= 10_000


def test_snapshot_passthrough_not_folded():
    """snapshot 是基线数据：不折叠、不产生语义事件。"""
    f = AudioDeviceFolder()
    out = f.feed(ev("snapshot", {"device_id": "A", "origin": "snapshot"}, 10.0), 10.0)
    out += f.tick(15.0)
    assert out == []


FIXTURE = Path(__file__).parent / "fixtures" / "audio_monitor_wake.jsonl"


def test_coordinated_flips_flag_churn():
    """协同指纹：多设备各翻 2 次（均低于单设备阈值 3）但同步发生 → churn。

    对应真实世界：显示器唤醒 = NVIDIA 多路端点各翻几次，单设备阈值抓不住。
    """
    f = AudioDeviceFolder(AudioFoldParams(settle_s=2.0, churn_flips=3,
                                          co_window_s=3.0, co_devices=2))
    out = []
    plan = [("A", 10.0), ("B", 10.4), ("C", 10.8),
            ("A", 11.0), ("B", 11.3), ("C", 11.5)]
    for did, ts in plan:
        out += f.feed(ev("state_changed", {"device_id": did, "state": "unplugged"}, ts), ts)
    assert out == []
    out += f.tick(14.0)
    assert len(out) == 3
    for e in out:
        assert e.payload["churn"] is True          # 单设备 flips=2，靠协同判 churn
        assert e.payload["co_flipping"] == 2


def test_real_monitor_wake_corpus():
    """真实语料回归：2026-10-25 晨显示器唤醒。

    原始形态：3 个 NVIDIA 端点（DP/HDMI 各一路）协同翻转，
    两段爆发（6 条 + 5 条）共 11 条 state_changed。
    折叠必须：不丢事件、每条定案带协同标记（churn=True）、最终 active。
    """
    events = [Event.from_json(json.loads(line))
              for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]
    assert len(events) == 11

    f = AudioDeviceFolder()
    out = []
    for e in events:
        out += f.feed(e, e.ts_mono)
    out += f.tick(events[-1].ts_mono + 3.0)

    settled = [e for e in out if e.type == "device_settled"]
    assert len(settled) == 6                       # 3 端点 × 2 段爆发
    assert sum(e.payload["flips"] for e in settled) == 11  # 折叠不丢事件
    assert len({e.payload["device_id"] for e in settled}) == 3
    assert all(e.payload["churn"] for e in settled)        # 协同指纹全部命中
    assert all(e.payload["co_flipping"] >= 2 for e in settled)
    assert settled[-1].payload["final_state"] == "active"  # 最终稳定在 active
