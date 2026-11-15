# 分层能力目录验收记录

日期：2026-11-14。后台为真实 Hermes + `deepseek-v4.1-flash-1017`；业务数据和控制结果全部模拟，未连接真实车辆。

## 配置与协议

- 28 个目录节点，顶层为车控、车书、车况。
- 50 个车控工具、8 个车书工具、10 个车况工具。
- 4 个模拟 profile；50 条车控、18 条查询、4 条跨域场景。
- 分文件 JSON、完整 bundle、目录/工具/profile/场景/bundle/结果 JSON Schema 已保存。
- Hermes 只看到五个固定 MCP 入口；系统提示不枚举业务工具或参数。

## 自动测试

当前 90 项自动测试通过，包括 50 条车控场景、所有查询工具在四套 profile 下的返回值校验，以及真实 MCP stdio 传输、中文目录过滤、深层目录、版本与任务快照、导入导出、失效预览、幂等、限流重试、取消、追问和通知时机。

依赖的 Starlette TestClient 输出一条 httpx 迁移提示，不影响当前测试结果。

## 真实模型验证

### 50 条车控场景

首轮严格评估 48/50，通过结果保存在 `test-results/live-catalog-controls-50.json`。另外两项已经针对性复测通过：

- C018：提交阶段失败，未生成任何工具结构；原始 HTTP 状态未记录。补充了 HTTP 错误状态事件与明确 429 的同幂等键退避重试，复测正常生成副驾座椅通风三档。
- C029：模型正确生成了“选择曲目 + 开始播放”两项操作，原预期只写了选择曲目。修正该场景的预期，并在工具配置中明确“选择不等于播放”（工具版本 1.0.1），复测通过。

50 条场景均有通过的真实模型记录；这不代表首轮零失败，也不保证所有改写表达都能成功。复测记录保存在 `test-results/live-catalog-followups.json`。

### 查询与跨域诊断

真实验证包括车书查询、车况查询、未配备空气悬架、接口未开放、驾驶条件不满足，以及查询车书后生成空调控制结构。

行驶中场景有一次返回 partial：原因已查清，但操作未完成，并附有明确错误证据和未完成项。这是有效的诊断结果，不是车控成功。评估允许这种语义，后续复测返回 unsupported，同样正确；记录在 `test-results/live-catalog.json`。

组合车控与缺参追问接续通过；记录在 `test-results/live-agent.json`。

### 只增加配置文件

临时加入 `control.climate.set_purification` 工具 JSON，重新加载后，真实 Hermes 发现了新工具并生成 `level=2`。没有修改 Agent 循环或 system prompt。验证后删除测试文件并重载，恢复原目录；该测试任务仍可读取其原目录快照。记录在 `test-results/live-config-extension.json`。

## 浏览器

桌面与手机宽度验收通过：分层浏览、按需显示 Schema、控制/查询结果区分、profile 切换、JSON 导出、非法导入拒绝、差异预览与应用。截图保存在 `test-results/catalog-desktop.png` 和 `test-results/catalog-mobile.png`。

分层目录版本已通过浏览器合成麦克风的真实语音回归：Qwen Audio 识别指令、Hermes 按目录生成参数、Qwen Audio 播报模拟成功，最后正常断开语音。截图为 `test-results/voice-desktop.png`，脚本为 `scripts/browser_voice_smoke.py`。测试没有录制实体麦克风；实际设备的噪声、回声和复杂插话仍需实测。

## 边界

Schema 校验验证参数与结果结构，不能证明模型对任意自然语言的理解或解释都正确。模拟车书不是实际车型手册，模拟状态不会因生成控制参数而改变。所有输出保留 `executed=false`。
