# 项目进度报告 #1（截至 2026-10-26 晚）

> 速率基准：单人业余 + AI 结对。项目启动于 2026-10-24 晚。
> 原方案估算 Phase 0 需 1.5–2 周；实际 **1 天**（AI 结对 + 范围收窄到 3+1 传感器）。

## 总览

| 维度 | 状态 |
|---|---|
| Phase 0（事件记录仪） | ✅ **完成并超额**：4+1 传感器（方案要求"全部"，我们收窄到音频/前台/电源会话/USB + 心跳），回放基建，7 天 soak 进行中 |
| Phase 0.5（入场券 A1/A2/A3） | ✅ 记忆库五表 DDL、音量归因决策表（含用户校准）、事件聚合层 |
| Phase 1 ①（手） | ✅ 音量执行器 + 音量观察传感器（context GUID 区分 agent/user）+ pre-arm 决策 |
| Phase 1 ②（脑） | ✅ 音量技能全闭环（触发/查询/分级执行/观察窗/归因/学习），**dry-run 影子模式运行中** |
| Phase 1 ③（脸） | ✅ 面板（http://127.0.0.1:7681：概览/偏好/台账/事件流 + 确认/拒绝/撤销/全局暂停）+ toast 通知（AUMID 已注册，带三键按钮） |
| 影子周配套（2026-10-26/27） | ✅ 三键校准闭环 + 周窗四象限/FA 率仪表 + 台账溯源回填 + 触发规则修订（§13/§14）+ 停留输出补决策 |
| pre-arm | ✅ **已开启（10-27 21:21）**：PrearmLedger 台账化 + 暂停覆盖反射层（§15）；agent 第一个真实动作待首次默认输出切换 |

## 度量快照（2026-10-27 晚）

- 事件流：**~6900 条**；测试 **89 个全绿**
- **三键校准全链路真机验证 ✅（20:41）**：toast 按钮 → knowbot: 协议 →
  CLI 落库 → 首条实例信念诞生（ZG2 → scalar 0.25，explicit/0.9/active）+
  首个台账裁决（#1 accept/correct_detection）+ 首个锚点。周误报率仪表
  开始积累（当前 0%，1 对 0 错，待裁决 1）
- 首批影子台账（20:30 ZG2 重插）：#1 扬声器触发正确；#2 麦克风误触
  （capture 排除缺失，当场修复，§13 附注）
- **ZG2 插拔会话三连验证**：STA 修复 ✅ / 事件链 ✅ / 触发+校准闭环 ✅
  ——开 pre-arm 的前置条件全部满足
- 记忆库：类级先验 4 条 + **ZG2 实例信念 1 条（用户第一个 explicit 信任）**

## Bug 战报（每个都是真机抓的，对策已沉淀进代码）

| # | 症状 | 根因 | 修复 |
|---|---|---|---|
| 1 | COM 回调注册参数转换失败 | comtypes 实现源接口须用 COMObject+QueryInterface 模式 | 改写注册路径 |
| 2 | form_factor 解出 "0:?" | 属性键 GUID 记错 | 遍历属性库实测纠正（`{1DA5D803-...-E0C0FFEE7F0E}` pid=0） |
| 3 | 一系列 AttributeError | Python 3.10 的 wintypes 缺 LRESULT/WNDCLASS 等 | 自定义结构体兜底层 |
| 4 | 批处理命令被腰斩 | Write 生成 LF 换行，cmd.exe 解析错乱 | CRLF 强制 + chcp 65001 |
| 5 | 音量回调永远不触发 | STA 泵只 GetMessage 不 Dispatch（跨套间回调靠消息分发） | 泵补 Dispatch |
| 6 | SetTimer 成功但计时器永不触发 | NULL-hwnd 线程计时器的 TimerProc 不走窗口分发 | 建 message-only 窗口挂计时器 |
| 7 | **拔出后音频事件全部静默消失** | 默认切换钩子在 COM 回调嵌套内反注册"消失中端点"的回调，跨进程 RPC 在 PnP 拆除窗口挂死 STA 线程 | 延迟 400ms 重注册（出回调上下文）+ 反注册单独容错 + 泵退出留痕 + 60s 线程心跳 |
| 8 | 音量事件缺 from 值 | 首个静止值无前值 | 传感器记录前一静止值；归因 N1 对无 from 跳过 |
| 9 | **toast"发送成功"但屏幕上什么都不显示** | 未注册 AUMID 的 Win32 程序调 toast Show() 不抛错但静默丢弃（PowerShell exit 0 是假阳性） | HKCU 注册 AppUserModelId（BurntToast 同款），daemon 启动幂等注册 |
| 10 | ZG2 麦克风（capture 端点）误触音量技能 | 触发放宽时只排除 hdmi/远程，漏了 capture；state_changed 元信息不带 flow | form_factor=microphone / flow=capture / MMDevice ID 前缀 `{0.0.1.` 三重排除（§13 附注） |

**战报的价值**：#7 是整个项目至今最重要的 bug——它证明了"传感器静默死亡"
不是理论风险，也催生了线程心跳（system.sensor/tick）这类可观测性基建。
所有这些形态的 bug 都已固化为回归测试或防御性代码。

## 真实数据带来的设计修正（详见 design-amendments.md）

1. 显示器唤醒 = **3 端点协同翻转**（不是单设备爆发）→ churn 改协同指纹；
2. 用户校准四问 → γ_content=0.95 / 30s 三档窗 / 60s 学习窗；
3. 真实插拔验证：USB 身份（VID/PID）与音频事件链符合预期，
   插回侧待 STA 修复的下次插拔确认。

## 下一步（按序）

1. **真机插拔会话**：验证 STA 修复 + 让影子模式产生第一批台账（顺手点一次
   toast 三键，验证协议回传全链路）
2. **影子周**：观察 dry-run 决策质量，周 FA 率 <10% 健康线读数，三键校准加速锚点
3. **开 pre-arm**（下次插拔验证通过后）→ agent 第一个真实动作
4. 关 dry-run → 音量技能真执行（通知档起步）
5. trickle：WiFi / 空闲（GetLastInputInfo）传感器
6. Phase 3：启动技能（节律记忆第一个用户）

## 运行方式（当前）

- 常驻：开机自启（启动文件夹 → VBS → 监护 cmd → daemon），日志 `data/daemon.log`
- 停止：`scripts\daemon_stop.cmd`；启动：`scripts\daemon_start.vbs`
- 观察：`.venv/Scripts/python -m knowbot tail/stats/dump/replay`
- 所有阈值：`config.toml`（自动加载）
