# Personal Agent · 分层车辆能力目录

车辆个人 Agent 的本机 Demo：千问 Audio Realtime 负责语音对话，Hermes 按需浏览车控、车书、车况目录，读取工具 Schema、生成参数并处理模拟结果。**没有车辆连接、车辆模拟器或真实车控执行代码。**

内置 **50 个车控工具、8 个车书工具、10 个车况工具、4 套模拟车辆配置和 72 条场景**。车控结构生成并校验后可播报“已模拟下发成功”；纯查询返回答案，不播报下发成功。模拟控制不会修改车况快照。

设计与协议见 [分层能力目录设计](docs/hierarchical-capability-catalog-design.md)。可交换的完整数据见 [JSON bundle](capabilities/exports/vehicle-capabilities.bundle.json)。

## 运行

需要 Python 3.12+ 与 uv：

```sh
uv sync --frozen
test -f .env || cp .env.example .env
uv run python scripts/serve_web.py
```

打开 <http://127.0.0.1:8765>。默认本机演示；配置手机入口后提供受配对保护的局域网HTTPS。一个数据库只允许一个应用实例；不要启用多 worker或再起独立手机后端。

没有凭据时可以打开“调用结构”，点击“查看预置示例”，检验 Schema、调用结构和界面。**预置示例是目录内的固定数据，没有调用模型，也不是自然语言理解的验证。** 车辆业务任务提交 Hermes；闲聊、焦点路由、明确取消和计划管理可由本地状态规则处理，不冒充模型执行或真实车辆动作。

## 聊天界面与步骤耗时

页面以 Agent 聊天形式展示用户指令、任务执行摘要和结果。每条回复的“处理过程”可以展开，显示候选预加载、目录浏览、Schema 读取、控制参数校验、模拟数据查询和结论提交。每个工具步骤按“操作 / 输入 / 结果”列出实际目录路径、筛选词、参数、模拟查询值和失败原因，可展开本步数据。旧记录从已保存的调用回执补充事实；未保存的路径、筛选词明确标为缺失。它展示实际工具事件，不读取或展示模型内部推理文本，也不额外调用模型来生成过程文案。

- **本地实测 / 工具实测**：使用单调时钟记录本地处理耗时。
- **模型 / 网络 / 调度区间**：从上一工具结束（或派单）到下一工具开始的间隔，不等于纯模型推理耗时。
- **用户等待**：追问到补充回答单列；正在进行的步骤和总耗时实时更新。
- **旧记录**：缺少工具计时的历史任务显示“阶段区间 · 含模型等待”，不冒充实测工具时间。

进度通过同会话 SSE 事件触发刷新，5 秒轮询兜底。展开状态、输入草稿和焦点保留。文字与语音使用同一任务焦点：短句默认关联最近用户交互的未结束任务，明确历史指代或真正歧义时再定位/选择，也可点击“新任务”明确另起请求。仍可用任务上的“补充信息”按钮指定目标。补充请求携带任务轮次，过期回答会被拒绝；目录、场景、Schema 和 JSON 位于侧栏抽屉。

候选预加载省掉的是目录发现轮次。默认启用“校验后直接完成”：满足单候选、首次单项完整控制等门槛时，模型生成参数并声明完整覆盖目标，本地校验后直接通知 S2S，不再请求模型确认。复杂任务与失败查证继续走原流程。六组成对简单指令测试中，中位后台耗时从 3.95 秒降到 2.17 秒，快速路径均只有一次提供方请求。详见[直接完成设计、门槛与实测限制](docs/direct-completion-design.md)。

成功任务的结论类型也由程序按实际引用回执判定：`finish_task` 可省略 `outcome`，自动区分纯车控、纯查询和混合结果；缺失回执、错误和未完成项仍需处理。这样不再为“纯控制/混合结果”标签错误重问模型。明确简单指令只带必要控制定义，其他请求保留关联查询与按需搜索。详见 [程序判定结论类型及实测](docs/result-classification-design.md)。

## 连接 Hermes

Hermes 是独立运行的服务，本项目通过其 Runs API 接入，不重复实现 Agent loop。请安装并配置支持 Runs API 的 Hermes 版本，在专用的演示配置中连接后台模型。不能只用一个文本生成模型端点冒充 Hermes API。

1. 根据 [Hermes 官方安装指南](https://hermes-agent.nousresearch.com/docs/getting-started/installation) 安装 Hermes，并完成模型配置。
2. 生成车辆 MCP 配置片段：

   ```sh
   uv run python scripts/hermes_mcp_config.py
   ```

   将输出的 `mcp_servers.vehicle_preview` 条目合并到 Hermes **专用演示配置**。输出是合法 JSON，也可作为 YAML 配置使用。工具会通过 stdio 运行本项目的 Python 环境。Hermes 与本项目需运行在同一台机器，并共用该 SQLite 路径。路径变动时重新生成配置；自定义 `PREVIEW_DB` 时，两端必须保持一致。

3. 在该 Hermes 配置中仅开启车辆预览 MCP 工具集，关闭无关的终端、文件、浏览器与其他工具。说明文字不是权限隔离；应通过 Hermes 自身工具配置限制能力。
4. 在 Hermes 的本地环境文件启用 API Server，设置 `API_SERVER_ENABLED=true` 和随机的 `API_SERVER_KEY`，然后启动 Hermes gateway。密钥保留在本地，不提交 Git，不粘贴到聊天中。
5. 在本项目 `.env` 设置 `HERMES_API_URL` 和对应的 `HERMES_API_KEY`，重启 Web 服务。

本项目用 `GET /v1/capabilities` 检查 Runs 支持，随后创建和查询任务，并按需停止。每个任务有独立 Hermes 会话，追问后的补充回到原会话。任务提交携带幂等键，结果状态未知时不自动重复提交。

Hermes 必须**实际调用目录协议取得校验通过的证据**才能记录完成；符合保守快速完成条件的单项控制由本地完成事务提交，其余任务仍需 `finish_task` 引用证据。只在最终文本中声称成功或输出 JSON，不会通过验收。部分参数已生成但任务失败时，保留结构供检查，不将整项任务标成成功。

Hermes 仅注册六个通用入口：`search_capabilities`、`browse_capabilities`、`describe_tools`、`call_tools`、`request_user_input`、`finish_task`。68 个业务工具由目录服务维护，不全量放进模型 tools 数组。系统提示不枚举业务工具名称或参数，模型按需取得 Schema。

默认采用**前置召回＋按需全局检索**：明确的单项固定目标先使用精简控制候选；相对调节等其他控制请求再加载同类别车况/车书查询定义及真实目录路径；其他请求尝试本地中文 BM25 召回。模型缺少能力时可调用 `search_capabilities`，一次取得少量候选及预算内的完整 Schema，直接调用业务工具。未命中或需要继续探索时仍可浏览目录。检索不查询真实或模拟车况，也不生成控制参数。详见 [工具发现优化与实测](docs/tool-discovery-design.md)。

早期仅预加载控制候选的版本，在同一模型与目录的56次成对测试中，两组均通过正确性断言；简单指令中位后台耗时由9.65秒降到3.82秒。完整数据、方法和限制见 [性能对比报告](docs/candidate-preload-benchmark.md)。该耗时不含S2S音频链路，不能当作整段语音延迟。

参考：[API Server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)、[MCP 配置](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)。

### 当前本机接入

已在项目 `.runtime/` 中安装隔离的 Hermes，源码版本为 `72c880370ff2e47dd587982ddded1b1ce90cfde4`，未修改其核心源码。后台模型按用户要求固定为 **`deepseek-v4.1-flash-1017`**，使用百炼 Anthropic 兼容地址。

后台模型与千问语音链路均已完成真实联调，业务结果始终为模拟数据、`executed=false`。分层目录的最新验证记录见 [验收记录](docs/capability-catalog-verification.md)。

本机重启方式：

```sh
# 终端一：使用已有 PM 环境启动 Hermes
uv run python scripts/start_hermes.py
# 终端二：启动预览服务
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8765 --no-access-log
```

`scripts/configure_local_hermes.py` 从项目 `.env` 读取凭据，生成专用 Hermes home，关闭外部登录借用、长期记忆及无关工具。它会更新本项目专用配置，不修改系统默认 Hermes home。修改 `BAILIAN_MODEL` 后运行该脚本并重启两个服务。

`hermes_plugins/bailian_limits/` 是一个本地兼容插件，通过 Hermes 的正式 `llm_request` 中间件将百炼 Anthropic 请求的输出上限限制为 8192 tokens。它解决默认输出上限过高造成的服务端拒绝，不替换 Agent loop，不改变工具选择。

如需在新环境复建：先按官方方式取得 Hermes 源码，再用其 PM 安装必需的工具与 `anthropic`、`mcp`、`web`、`messaging` 依赖组。`messaging` 提供 API Server 所需的 aiohttp，并不意味着开启聊天平台。运行配置脚本后启动服务即可。

## 连接千问 Audio Realtime

在本机 `.env` 设置：

| 变量 | 作用 |
| --- | --- |
| `QWEN_AUDIO_API_KEY` | 可选的独立 S2S Key，优先使用，仅留在后端 |
| `DASHSCOPE_API_KEY` | 未设置独立 S2S Key 时的兼容凭据 |
| `BAILIAN_WORKSPACE_ID` | 可选；填写时使用对应业务空间域名，留空使用地域公共端点 |
| `BAILIAN_REGION` | 北京 `cn-beijing` 或新加坡 `ap-southeast-1` |
| `QWEN_AUDIO_MODEL` | 默认 `qwen-audio-3.1-realtime-flash`，这是可修改的实现默认值 |
| `QWEN_AUDIO_VOICE` | 默认 `longanqian_v3.1`，必须与所选模型兼容 |
| `QWEN_AUDIO_PROXY` | 可选代理地址，默认直连，不继承临时开发代理 |

重启服务后，浏览器点击“开始语音”并允许麦克风。浏览器将单声道 16 kHz PCM16 音频送到本地服务，本地服务代理百炼 WebSocket；模型音频按 24 kHz 播放。浏览器不持有百炼或 Hermes 的凭据。

S2S 只看到派单、补充信息、取消、查询任务四种工具。车辆工具全部放在 Hermes 后面。普通语音插话只停止播放，不取消后台任务。S2S 连接时会静默加载当前待答任务的目标、问题和轮次，让“全部”等简短回答能关联原任务；不为这些历史上下文主动生成回复。语音模式的普通文字输入仍交给 S2S 区分补充、闲聊或新需求，只有明确手选回复目标才直接发送补充接口。

后台完成或需要追问时，结果先保存在数据库，再由适配层在用户、模型和播放器都空闲时注入会话并请求语音回复。新语音连接从当前事件游标开始接收通知，**不会自动补播连接前已完成的文字任务或旧成功通知**；连接前仍在执行、连接后才完成的任务正常通知。同一次通话中打断播报不确认送达，空闲后可以重播；跨连接的旧结果仍可在任务记录查看。断开语音不会取消后台任务。

页面独立显示“S2S 过程”：连接、语音转写文本、派单目标、补充/取消操作、后台结果和参数回传、模型回复内容、浏览器播放状态及确认送达。耗时为各阶段观测区间（可能重叠），不等于纯推理或纯 ASR 耗时；只记录本次页面中的语音连接，不保存原始麦克风音频。

当前选型为 `qwen-audio-3.1-realtime-flash` 与 `longanqian_v3.1`，使用北京公共端点，无需业务空间 ID。官方明确 Audio Realtime 的原公共域名仍可用，见 [WebSocket 接入说明](https://help.aliyun.com/en/model-studio/audio-realtime-websocket-access)。业务空间 ID 为可选；应使用与 Key 匹配的模型和地域。新独立 S2S Key 对 Plus 返回 `Model.AccessDenied`，按用户指定改为 Flash 后已通过真实语音连接验证。

已用本机生成的固定中文语音跑通真实音频输入 → Qwen Audio 派单 → Hermes/DeepSeek 参数生成 → Qwen Audio 语音播报模拟成功，并通过浏览器合成麦克风、AudioWorklet 与播放链路验收。测试未录制真实麦克风。实体麦克风的权限、噪声与回声表现仍需在使用设备上确认。

参考：[Audio 指南](https://help.aliyun.com/en/model-studio/qwen-audio-realtime-user-guides)、[客户端事件](https://help.aliyun.com/en/model-studio/fun-audiochat-client-events)。

## 增加与更新工具

```text
capabilities/
  manifest.json             包信息与格式版本
  directories/              目录节点：path、title、description、aliases
  tools/control/            50个车控工具，每工具一个 JSON
  tools/manual/             8个模拟车书工具
  tools/status/             10个模拟车况工具
  profiles/                 配置完整、无空气悬架、接口缺失、行驶中
  scenarios/                50车控 + 18查询 + 4跨域场景
  schemas/                  目录、工具、profile、场景、bundle、结果协议
  exports/                  初始配置的完整交换文件
```

新增已有类别下的工具：

1. 参考同类别工具文件，新增一个 JSON，设置独立的 `id`、`version`、`category_path`。
2. 填写用途、别名、`input_schema`、`output_schema`、`mock` 和 `examples`。
3. 执行 `uv run python scripts/validate_capabilities.py` 检查配置。
4. 页面点击“重新加载文件”。新任务即可浏览和调用，无需修改代码、MCP 注册或 system prompt。

新增类别时，还需增加目录节点 JSON。修改已有工具内容必须递增工具版本；新目录快照不会影响已有任务及其追问接续。

mock 支持四种声明式模式：车控 `prepare_only`、固定结果 `static`、按参数查表 `argument_lookup`、读取模拟 profile 的 `profile_pointer`。业务前置条件通过 `preconditions` 声明。配置不支持代码执行、任意 HTTP 请求或远程 Schema 引用。

工具配置决定模拟行为；接入新的真实设备协议仍需实现对应适配器，不是增加 JSON 就能控制真车。旧 `catalog/` 文件仅保留为历史任务兼容，不再是新任务的数据源。

## 导入导出

- **导出 JSON**：下载当前目录完整 bundle，包含目录、工具、profile 和场景。
- **导入配置**：可选完整 bundle 或单个工具 JSON，先预览差异，再点击应用。
- **增加 / 合并**：允许新增和完全相同的重复项；同 ID 内容冲突则拒绝。
- **更新已有项**：显式 upsert，已有工具必须递增 version。
- **完整替换**：仅接受完整 bundle，预览删除项后应用。

导入整体校验，失败不生效；成功时原子发布并保留旧文件备份。旧快照保存在任务数据库；源文件备份位于 `.data/catalog-backups/`。未完成的旧版任务只供历史查看，需新建任务使用目录协议。

命令行校验和导出：

```sh
uv run python scripts/validate_capabilities.py
uv run python scripts/validate_capabilities.py --export /tmp/vehicle-capabilities.bundle.json
```

缺少参数时经前台追问，再继续原任务。每项任务最多 64 次业务调用、最终最多 8 个不同车控预览；默认两分钟时限，Hermes 上限 24 轮。终态包括预览完成、查询完成、混合结果、不支持、部分完成和失败，均关联实际 result_id。

## 测试

```sh
uv run pytest -q
node --check web/app.js
node --check web/pcm-worklet.js
```

本机服务运行后可以执行浏览器验收：

```sh
uv run python scripts/browser_catalog_smoke.py
```

浏览器脚本优先使用 macOS 上已安装的 Chrome；其他系统需要先执行 `uv run playwright install chromium`。目录浏览器脚本使用预置数据，不产生真实模型调用，截图保存在 `test-results/`。

测试覆盖实际 MCP stdio 传输、Schema 校验、追问接续、幂等、取消、隔离、通知时机和 Hermes HTTP 合约。直接完成测试还覆盖并行工具、漏声明、失败/追问、取消、幂等、提供方请求抑制和 S2S 空闲门控。Hermes HTTP 合约测试使用明确的测试替身，不能代替真实 Hermes + 模型联调。

真实模型验收会产生推理费用，不会执行车控：

```sh
uv run python scripts/live_catalog_smoke.py
uv run python scripts/live_catalog_smoke.py --all-controls --concurrency 2
uv run python scripts/live_agent_smoke.py
# 聊天 UI、逐步计时、追问与失败查证（3 条真实指令）
uv run python scripts/browser_chat_smoke.py
uv run python scripts/benchmark_preload.py --repeats 2
# 固定候选预加载，单独对照自动完成与模型确认
uv run python scripts/benchmark_direct_completion.py
```

前两项分别检查代表场景和完整50条车控场景，后一项检查组合指令及追问接续。只验证凭据与工具协议可运行 `uv run python scripts/check_model_connection.py`。脚本均不输出密钥。

语音相关验收（会产生少量实时模型调用）：

```sh
# 会话配置与真实语音输出
uv run python scripts/check_voice_connection.py
# 完整 S2S 与 Hermes 链路；需要 test-results/voice-command.wav
uv run python scripts/live_voice_smoke.py
# 浏览器合成麦克风、AudioWorklet 和播放链路
uv run python scripts/browser_voice_smoke.py
# 先创建旧成功通知，再静默连接；随后输入新指令并检查过程与实际播报
uv run python scripts/browser_process_voice_smoke.py
# 追问后自动接续、刷新恢复、语音接续及多任务选择
uv run python scripts/browser_clarification_smoke.py
# 无预加载时的真实全局工具搜索与页面检查
uv run python scripts/browser_tool_search_smoke.py
```

测试输入需为 16 kHz、单声道、16-bit PCM WAV；固定测试句为“把所有分区的空调温度设置成二十三度”。测试音频与结果只保存在被忽略的 `test-results/` 中。

## 文件结构

```text
app/                 Web 服务、Hermes 适配、语音桥、任务存储、MCP 工具
capabilities/        分层目录、工具、模拟配置、场景与交换文件
protocols/           稳定结果协议模板
catalog/             旧版历史任务兼容
web/                 无构建步骤的浏览器界面与 PCM 音频处理
tests/               自动测试
scripts/             MCP 配置输出与浏览器验收
docs/                两套设计方案
```

`uv.lock` 固定本次解析的依赖。`.env`、运行数据库和测试截图均被忽略。服务重启后将进行中的任务标为中断，不自动重放；等待补充信息的任务五分钟后过期。


## 手机页面与双向同步

手机入口为 `/mobile`，电脑仍使用 `/`。桌面点击“连接手机”生成五分钟有效、一次使用的12位配对码；手机输入后，两端共享会话、消息、任务焦点和周期计划。无需把模型密钥放进手机。

第一次准备局域网访问：

```sh
uv run python scripts/configure_mobile_access.py
uv run python scripts/start_local_services.py
```

如果网页服务已在运行，需要先正常重启该网页进程，使其加载新地址和TLS证书。不要启动第二个独立应用来访问同一个数据库。配置脚本输出本机手机入口与连接说明地址；也可从电脑“连接手机”面板打开。

手机与电脑连接同一局域网，按连接说明手动信任本项目本地CA后使用HTTPS页面和麦克风。本工具**不会自动安装系统信任**，不会安装开机自启；CA私钥只保存在忽略的 `.runtime/mobile-tls`，公开下载仅提供证书。网络/IP变化后重新运行配置脚本并重启网页服务，同一CA可复用，新地址可能需要重新配对。

手机只展示简洁对话、追问选项、结果和活动/计划。电脑同步显示完整可观测过程、耗时、Schema与调用结构。读取消息不确认语音播放；一个会话只允许一个语音端，切换需显式接管。解除设备或重置连接会使旧凭据失效。

- [手机需求](docs/mobile-personal-assistant-requirements.md)
- [双端设计与实现](docs/mobile-personal-assistant-design.md)
- [连接步骤与验收记录](docs/mobile-sharing-acceptance.md)

验收脚本：

```sh
uv run python scripts/browser_mobile_contract_smoke.py
uv run python scripts/browser_desktop_sharing_smoke.py
uv run python scripts/validate_mobile_sharing.py
uv run python scripts/validate_shared_voice.py --audio
```

真实模型脚本使用独立新测试会话且仅生成模拟车控。录音验收需要已准备的16kHz单声道PCM WAV；它不等于实体手机麦克风测试。
