# knowbot —— 个人驻留事件 Agent

设计文档见 [个人驻留事件Agent-项目方案.md](./个人驻留事件Agent-项目方案.md)。

当前进度：**Phase 0 事件记录仪**（只听不动作）。daemon 骨架 + 事件信封 +
SQLite 落库 + 心跳/音频/前台/电源会话传感器 + 回放基建。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/pip install -e .            # Windows；*nix 用 .venv/bin/
.venv/Scripts/python -m knowbot daemon    # 常驻记录（在仓库根目录运行）
```

全部命令：

| 命令 | 作用 |
|---|---|
| `daemon [--duration N]` | 常驻记录；`--duration` 秒数自停（冒烟测试） |
| `tail [-n 20]` | 最近 N 条事件 |
| `stats` | 每传感器计数与时间范围 |
| `dump [--from --to] --out f.jsonl` | 导出 golden file（回放语料） |
| `replay --file f.jsonl [--speed 0]` | 回放校验：保序、mono 单调、会话切分 |

可选：`cp config.example.toml config.toml` 调整阈值（数据库路径、心跳周期等）。

## 传感器（Phase 0 首批 3 + 心跳）

| 命名空间 | 内容 | 线程 |
|---|---|---|
| `system.heartbeat` | 每 60s 一拍：存活证明 + 缺口探测 | asyncio 任务 |
| `audio.device` | 端点插拔/默认设备切换/启动快照（id、友好名、form factor、流向、角色） | 独立 STA 线程（COM 回调） |
| `app.foreground` | 前台应用切换（仅 exe 名 + pid） | 消息泵线程（WinEventHook） |
| `system.power` / `system.session` | 睡眠/唤醒、锁屏/解锁（含 `clock_reconcile` 时钟对账） | 同一消息泵线程（隐藏窗口） |

待接入（soak 期间 trickle in）：USB（WM_DEVICECHANGE）、蓝牙、应用安装。

## 数据与隐私

- `event_raw` 只存标识（设备 ID、exe 名、pid），**不截屏、不读窗口标题、不监听键盘**。
- 单个 SQLite 文件（WAL）：`data/knowbot.db`，90 天保留（清理工具待做）。
- 队列满即丢弃计数，绝不反压（保护 COM 回调线程）。

## Phase 0 纪律（不做什么）

无决策层、无任何动作（连 pre-arm 都不碰）、无面板、无模型、无桌宠、不打包。
验收：连续 7 天零人工干预，心跳无缺额，三类事件齐全可回放。
