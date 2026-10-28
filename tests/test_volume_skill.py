"""② 音量技能闭环黄金测试：Fake 执行器 + 临时库 + 真 asyncio 总线。

三场景：影子窗学习（missed_need→草稿）、接受升置信、纠正降置信+锚点。
"""
import asyncio
import json

from knowbot.attribution.volume import MeterStats, VolumeAttributionParams
from knowbot.bus import EventBus
from knowbot.envelope import Event
from knowbot.learning.learner import VolumeLearner
from knowbot.skills.volume import VolumeSkill
from knowbot.store.db import EventStore
from knowbot.store.memory import MemoryStore, seed_defaults


class FakeExecutor:
    def __init__(self, initial=0.8):
        self.initial = initial
        self.current = initial
        self.set_calls = []

    def get(self, device_id):
        return self.current

    def set(self, device_id, value):
        before, self.current = self.current, value
        self.set_calls.append((device_id, value))
        return before


class FakeMeter:
    def __init__(self, stats):
        self._stats = stats

    def start(self): pass

    def stop(self):
        return self._stats


DEV = "{test-device}"
INST = f"audio.device:{DEV}"
CLS = "audio.form_factor:headphones"

P = VolumeAttributionParams(watch_window_s=0.3, c_window_s=0.3, strong_band_s=2.0,
                            normal_band_s=10.0)


def settled_event(ff="headphones", name=None):
    payload = {
        "device_id": DEV, "form_factor": ff, "final_state": "active",
        "flips": 2, "churn": False,
    }
    if name:
        payload["name"] = name
    return Event("audio.flow", "device_settled", payload)


def switch_event(role="multimedia", ff="headphones"):
    """手动切换默认输出（不插拔）→ default_output 语义事件。"""
    return Event("audio.flow", "default_output", {
        "device_id": DEV, "flow": "render", "role": role,
        "form_factor": ff, "repeats": 1,
    })


def user_adjust(scalar, frm=0.45, delta=0.15):
    return Event("audio.volume", "changed", {
        "device_id": DEV, "scalar": scalar, "from": frm, "actor": "user",
        "burst": 3, "knob": "master",
    }, ts_mono=__import__("time").monotonic())


def _setup(tmp_path):
    store = EventStore(tmp_path / "s.db", batch_size=10_000, flush_interval=0.05)
    memory = MemoryStore(store._conn)
    seed_defaults(memory)
    return store, memory


async def _drive(bus, skill, script, settle=0.6):
    """启动总线分发 → 按 script[(delay, event)] 注入事件 → 等观察窗结束。"""
    import time as _t
    runner = asyncio.create_task(bus.run())
    t0 = _t.monotonic()
    for delay, item in script:
        await asyncio.sleep(max(0.0, delay - (_t.monotonic() - t0)))
        bus.publish(item() if callable(item) else item)
    await asyncio.sleep(settle)
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)


def test_scenario_shadow_learns_from_user(tmp_path):
    """无实例信念 + dry-run：技能决策进台账；用户随后手调 → missed_need → 草稿信念诞生。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())

        await _drive(bus, skill, [(0.05, settled_event()),
                                  (0.35, user_adjust(0.62, frm=0.35))])
        skill.stop()

        row = memory.conn.execute(
            "SELECT exec_mode, action FROM action_log ORDER BY id DESC").fetchone()
        assert row["exec_mode"] == "dry_run"
        assert json.loads(row["action"])["after"] == 0.35  # 类级先验 scalar

        # missed_need：用户停在 0.62 → 实例草稿信念诞生
        draft = memory.get("audio.volume", "instance", INST)
        assert draft is not None and draft["state"] == "draft"
        assert json.loads(draft["value"])["scalar"] == 0.62
        assert draft["source"] == "observed"

        log_row = memory.conn.execute(
            "SELECT reaction, quadrant FROM action_log ORDER BY id DESC").fetchone()
        assert log_row["reaction"] == "missed_need"
        assert log_row["quadrant"] == "missed_need"
        n_ev = memory.conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE pref_id=? AND kind='event'",
            (draft["id"],)).fetchone()[0]
        assert n_ev == 1
        store.close()

    asyncio.run(main())


def test_scenario_accept_raises_confidence(tmp_path):
    """真执行（notify 档）+ 窗口内没碰 → accept → 置信度上调。"""
    async def main():
        store, memory = _setup(tmp_path)
        # 实例级高置信信念 0.75 → notify 档真执行
        memory.get_or_create("audio.volume", "instance", INST,
                             {"scalar": 0.40}, 0.75, "explicit")
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=False, meter_factory=None)
        skill.start(asyncio.get_running_loop())

        await _drive(bus, skill, [(0.05, settled_event())])  # 不做任何调整
        skill.stop()

        assert ex.set_calls == [(DEV, 0.40)]        # 真的执行了
        pref = memory.get("audio.volume", "instance", INST)
        assert pref["confidence"] > 0.75            # 接受上调（0.75+0.15*0.25≈0.7875）
        row = memory.conn.execute(
            "SELECT reaction, quadrant, exec_mode, latency_ms FROM action_log"
            " ORDER BY id DESC").fetchone()
        assert row["exec_mode"] == "notify"
        assert row["reaction"] == "accept" and row["quadrant"] == "correct_detection"
        assert row["latency_ms"] is not None
        store.close()

    asyncio.run(main())


def test_scenario_correction_and_anchor(tmp_path):
    """真执行后用户反向调整（安静）→ 强纠正 → 置信度下降 + 锚点写入设备映射。"""
    async def main():
        store, memory = _setup(tmp_path)
        memory.get_or_create("audio.volume", "instance", INST,
                             {"scalar": 0.40}, 0.75, "explicit")
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        quiet = {"mean": 0.02, "peak": 0.04, "active_frac": 0.05}
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=False, meter_factory=lambda d: FakeMeter(quiet))
        skill.start(asyncio.get_running_loop())

        # agent 0.8→0.40 下调；用户 0.40→0.70 反向上调（安静中）→ A4
        await _drive(bus, skill, [(0.05, settled_event()),
                                  (0.35, user_adjust(0.70, frm=0.40))])
        skill.stop()

        pref = memory.get("audio.volume", "instance", INST)
        assert pref["confidence"] < 0.75            # 被纠正下调
        row = memory.conn.execute(
            "SELECT reaction, quadrant FROM action_log ORDER BY id DESC").fetchone()
        assert row["reaction"] == "correct" and row["quadrant"] == "false_alarm"

        dmap = memory.get("audio.device_map", "instance", INST)
        assert dmap is not None                     # 锚点已写设备映射
        anchors = json.loads(dmap["value"])["anchors"]
        assert anchors[-1][1] == 0.70

        corr = memory.conn.execute(
            "SELECT detail FROM evidence WHERE kind='correction'"
            " ORDER BY id DESC").fetchone()
        assert "A4" in corr["detail"]               # 规则编号进证据（可解释引用）
        store.close()

    asyncio.run(main())


def test_manual_switch_triggers_skill(tmp_path):
    """手动切换默认输出到耳机（不插拔）→ 技能照常决策（意图等价于插入）。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        await _drive(bus, skill, [(0.05, switch_event())])
        skill.stop()
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 1
        row = memory.conn.execute("SELECT exec_mode, action FROM action_log").fetchone()
        assert row["exec_mode"] == "dry_run"
        assert json.loads(row["action"])["device_id"] == DEV
        store.close()

    asyncio.run(main())


def test_plugin_chain_triggers_once(tmp_path):
    """插拔会同时产生 settled + default_output → 去重后只触发一次决策。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        await _drive(bus, skill, [(0.05, settled_event()),
                                  (0.10, switch_event())])
        skill.stop()
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 1  # 不重复决策
        store.close()

    asyncio.run(main())


def test_console_role_switch_ignored(tmp_path):
    """console 角色的默认切换不触发（只跟 multimedia 主听音角色）。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        await _drive(bus, skill, [(0.05, switch_event(role="console"))])
        skill.stop()
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 0
        store.close()

    asyncio.run(main())


def test_finalize_backfills_trigger_event_id(tmp_path):
    """台账溯源链：finalize 反查触发事件 id 回填（"你为什么信这个"走到原始事件）。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        bus.subscribe("", store.append)              # 生产装配：store 先订阅
        flusher = asyncio.create_task(store.run())   # 周期落库（finalize 时已可见）
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        await _drive(bus, skill, [(0.05, settled_event())])
        skill.stop(); flusher.cancel()
        await asyncio.gather(flusher, return_exceptions=True)

        row = memory.conn.execute(
            "SELECT trigger_event_id FROM action_log ORDER BY id DESC").fetchone()
        ev = memory.conn.execute(
            "SELECT id FROM event_raw WHERE sensor='audio.flow'").fetchone()
        assert ev is not None                       # 触发事件已落库
        assert row["trigger_event_id"] == ev["id"]  # 台账回填了同一事件
        store.close()

    asyncio.run(main())


def test_monitor_and_churn_do_not_trigger(tmp_path):
    """显示器端点（hdmi）与抖动端点（churn）不触发技能（A3 触发条件）。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=False, meter_factory=None)
        skill.start(asyncio.get_running_loop())

        hdmi = Event("audio.flow", "device_settled", {
            "device_id": "{hdmi-dev}", "form_factor": "hdmi",
            "final_state": "active", "flips": 2, "churn": False})
        churn = Event("audio.flow", "device_settled", {
            "device_id": "{churn-dev}", "form_factor": "headphones",
            "final_state": "active", "flips": 6, "churn": True})
        await _drive(bus, skill, [(0.05, hdmi), (0.1, churn)])
        skill.stop()

        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 0 and ex.set_calls == []        # 一次出手都没有
        store.close()

    asyncio.run(main())


# ---------- 2026-10-27 触发规则修订：成为听音输出皆触发（design-amendments §13） ----------
# 实证：ZhuAudio ZG2 USB DAC 接耳机，Windows 报 speakers，耳机类白名单漏掉。

def test_speakers_settled_triggers(tmp_path):
    """非耳机类端点插拔定案（ZG2 场景）：同样触发，类级先验取 speakers。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        await _drive(bus, skill, [(0.05, settled_event(ff="speakers", name="扬声器 (ZhuAudio ZG2)"))])
        skill.stop()
        row = memory.conn.execute(
            "SELECT exec_mode, action, pref_id FROM action_log ORDER BY id DESC").fetchone()
        assert row is not None and row["exec_mode"] == "dry_run"
        assert json.loads(row["action"])["after"] == 0.25  # speakers 类级先验
        pref = memory.get_pref(row["pref_id"])
        assert pref["scope_key"] == "audio.form_factor:speakers"
        store.close()

    asyncio.run(main())


def test_speakers_default_output_triggers(tmp_path):
    """手动把默认输出切到 speakers 类（ZG2 插入时 churn 连累 settled 的兜底路径）。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        await _drive(bus, skill, [(0.05, switch_event(ff="speakers"))])
        skill.stop()
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 1
        store.close()

    asyncio.run(main())


def test_remote_and_missing_form_factor_excluded(tmp_path):
    """远程桌面虚拟端点与元信息缺失的端点不触发。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        remote = Event("audio.flow", "device_settled", {
            "device_id": "{rdp}", "form_factor": "remote_network_device",
            "final_state": "active", "flips": 1, "churn": False})
        no_ff = Event("audio.flow", "default_output", {
            "device_id": "{no-meta}", "flow": "render", "role": "multimedia"})
        await _drive(bus, skill, [(0.05, remote), (0.10, no_ff)])
        skill.stop()
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 0
        store.close()

    asyncio.run(main())


def test_capture_endpoints_do_not_trigger(tmp_path):
    """capture 端点不是听音输出（2026-10-27 首次真实触发实测：ZG2 麦克风误触）。
    state_changed 元信息不带 flow，按 MMDevice ID 前缀 {0.0.1. 识别 capture。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        mic_settled = Event("audio.flow", "device_settled", {
            "device_id": "{0.0.1.00000000}.{a920d63c-d326-4140-895a-c90dbd848015}",
            "form_factor": "microphone", "final_state": "active",
            "flips": 1, "churn": False})
        mic_default = Event("audio.flow", "default_output", {
            "device_id": "{0.0.1.00000000}.{a920d63c}", "flow": "capture",
            "role": "multimedia", "form_factor": "microphone"})
        await _drive(bus, skill, [(0.05, mic_settled), (0.10, mic_default)])
        skill.stop()
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 0
        store.close()

    asyncio.run(main())


def test_resting_default_gets_pending_decision(tmp_path, monkeypatch):
    """快速来回切换：观察窗内的重复触发被挡但不丢——停轮后对停留输出补决策。
    （2026-10-27 实测缺口：最终停留的输出可能永远无决策。）"""
    from knowbot.skills import volume as skill_mod
    monkeypatch.setattr(skill_mod, "TRIGGER_DEDUP_S", 0.03)  # 模拟快速切换

    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        ex = FakeExecutor(initial=0.8)
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), ex, P,
                            dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())

        dev_b = "{realtek-dev}"
        switch_b = Event("audio.flow", "default_output", {
            "device_id": dev_b, "flow": "render", "role": "multimedia",
            "form_factor": "speakers", "repeats": 1})
        # 0.05 ZG2 定案 → 决策#1（watch 0.3s）；0.10 切到 B → 决策#2；
        # 0.18 切回 ZG2 → 被 ZG2 观察窗挡下记 pending；0.35 窗到期 → 补决策#3
        await _drive(bus, skill, [(0.05, settled_event(ff="speakers")),
                                  (0.10, switch_b),
                                  (0.18, switch_event(ff="speakers"))],
                     settle=0.6)
        skill.stop()

        rows = memory.conn.execute("SELECT action FROM action_log ORDER BY id").fetchall()
        devs = [json.loads(r["action"])["device_id"] for r in rows]
        assert devs == [DEV, dev_b, DEV]     # 停留输出最终拿到决策
        store.close()

    asyncio.run(main())


# ---------- 计算礼貌（哲学 9）：影子 toast 节流（2026-10-27 23:10 风暴实证） ----------

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, title, body, actions=None):
        self.calls.append(title)


def test_shadow_toast_device_cooldown(tmp_path):
    """同设备冷却期内重复决策照常落台账，但只弹一次通知。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        rec = _Recorder()
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), FakeExecutor(), P,
                            dry_run=True, meter_factory=None, notifier=rec)
        skill.start(asyncio.get_running_loop())
        # 两次触发间隔 > 观察窗（0.3s），第二次是全新决策——但冷却 600s 内
        await _drive(bus, skill, [(0.05, settled_event()),
                                  (0.50, settled_event())], settle=0.5)
        skill.stop()
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 2                       # 决策一条不少
        assert len(rec.calls) == 1          # 通知只弹一次
        store.close()

    asyncio.run(main())


def test_shadow_toast_daily_budget(tmp_path):
    """全局日预算：跨设备也受总额限制，超限静默落台账。"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        rec = _Recorder()
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), FakeExecutor(), P,
                            dry_run=True, meter_factory=None, notifier=rec,
                            toast_daily=1)
        skill.start(asyncio.get_running_loop())
        dev_b = Event("audio.flow", "default_output", {
            "device_id": "{other-dev}", "flow": "render", "role": "multimedia",
            "form_factor": "speakers", "repeats": 1})
        await _drive(bus, skill, [(0.05, settled_event()),
                                  (0.15, dev_b)], settle=0.5)
        skill.stop()
        n = memory.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        assert n == 2
        assert len(rec.calls) == 1          # 预算 1：第二台设备静默
        store.close()

    asyncio.run(main())


def test_stale_trigger_for_departed_default_skipped(tmp_path):
    """时效校验：切走后到达的旧设备决策作废——只有当前默认输出得到决策。
    （2026-10-27 实测竞态：切到新设备后，toast 仍在说旧设备。）"""
    async def main():
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        rec = _Recorder()
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), FakeExecutor(), P,
                            dry_run=True, meter_factory=None, notifier=rec)
        skill.start(asyncio.get_running_loop())

        dev_b = "{dev-b}"
        raw = lambda d: Event("audio.device", "default_changed",
                              {"device_id": d, "flow": "render", "role": "multimedia"})
        flow = lambda d: Event("audio.flow", "default_output", {
            "device_id": d, "flow": "render", "role": "multimedia",
            "form_factor": "speakers", "repeats": 1})
        # 0.05 切到 B；0.15 B 折叠定案 → 决策 B
        # 0.55 切回 A（真相更新）；0.60 折叠层滞后吐出 B 的旧窗口 → 必须作废
        # 0.70 A 折叠定案 → 决策 A
        await _drive(bus, skill, [
            (0.05, raw(dev_b)), (0.15, flow(dev_b)),
            (0.55, raw(DEV)), (0.60, flow(dev_b)), (0.70, flow(DEV)),
        ], settle=0.5)
        skill.stop()

        rows = memory.conn.execute("SELECT action FROM action_log ORDER BY id").fetchall()
        devs = [json.loads(r["action"])["device_id"] for r in rows]
        assert devs == [dev_b, DEV]          # 过期的 B 没有第二次决策
        assert len(rec.calls) == 2           # 也没有第二次提示
        store.close()

    asyncio.run(main())


def test_c_window_learns_final_value_with_trailing_finalize(tmp_path):
    """C 窗尾随定案（2026-10-28）：用户微调持续覆盖（末值学习），调完静默
    c_trailing_s 即收尾——不再等满 60s 窗，草稿取最终值而非首次粗调值。"""
    async def main():
        from knowbot.attribution.volume import VolumeAttributionParams as VAP
        p = VAP(watch_window_s=2.0, c_window_s=2.0, c_trailing_s=0.4,
                strong_band_s=2.0, normal_band_s=10.0)
        store, memory = _setup(tmp_path)
        bus = EventBus(); bus.bind(asyncio.get_running_loop())
        skill = VolumeSkill(bus, memory, VolumeLearner(memory), FakeExecutor(),
                            p, dry_run=True, meter_factory=None)
        skill.start(asyncio.get_running_loop())
        # 0.05 触发开 C 窗（窗 2.0s）；0.2 调到 0.5；0.5 微调到 0.35（末值）
        # → early_at = 0.5+0.3 = 0.8 < 窗尾 2.05，提前定案
        # handset 无类级先验 → 落到 global(0.30) < 建议档 → 无信念 C 窗
        await _drive(bus, skill, [
            (0.05, settled_event(ff="handset")),
            (0.20, lambda: user_adjust(0.50, frm=0.25)),
            (0.50, lambda: user_adjust(0.35, frm=0.50))],
            settle=1.2)
        skill.stop()

        draft = memory.get("audio.volume", "instance", INST)
        assert draft is not None and draft["state"] == "draft"
        assert json.loads(draft["value"])["scalar"] == 0.35   # 末值，非首次 0.50
        row = memory.conn.execute(
            "SELECT reaction FROM action_log ORDER BY id DESC").fetchone()
        assert row["reaction"] == "missed_need"
        store.close()

    asyncio.run(main())
