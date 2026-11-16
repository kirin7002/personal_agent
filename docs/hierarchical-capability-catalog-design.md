# 分层车辆能力目录设计 v1

日期：2026-11-14  
状态：协议、示例配置和功能代码已落地；验收结果见 `capability-catalog-verification.md`。  
范围：继续使用 Hermes + DeepSeek；实现配置驱动的车控、车书、车况目录。前台沿用千问 S2S 和文字入口。所有数据为模拟数据，不连接真实车辆。

## 1. 目标与责任边界

新增一个已有类别下的工具，只需新增工具 JSON 文件并重新加载目录，不修改 Agent 循环、MCP 注册代码或 system prompt。新增类别时，再增加对应目录描述。真实设备或新协议适配仍需后续实现，不能仅凭配置获得不存在的真实执行能力。

| 部分 | 负责什么 |
| --- | --- |
| Hermes | 理解目标，选择目录与工具，生成参数，处理查询结果，决定继续、追问或结束 |
| 能力目录服务 | 层级浏览、工具详情、配置版本、模拟器选择、参数和输出校验 |
| 工具 JSON | 名称、用途、类别、参数 Schema、结果 Schema、模拟规则、使用示例 |
| 任务服务 | 固定目录快照、任务轮次、调用记录、取消与结果回传 |
| 前台 | 语音/文字输入、追问、结构展示、空闲时播报 |

当前工具发现支持本地前置召回、关联查询定义预加载和按需全局检索；详见 [工具发现优化](tool-discovery-design.md)。

目录层级是导航结构，不是只能选一个分支的分类器。一个任务可同时选择多个类别，也可在车控失败后浏览车书、车况。

## 2. 能力层级

```text
根目录
├── control / 车控
│   ├── climate / 空调
│   ├── windows / 车窗
│   ├── seats / 座椅
│   ├── multimedia / 多媒体
│   ├── lights / 灯光
│   ├── mirrors / 后视镜
│   ├── suspension / 悬架
│   └── sunroof / 天窗
├── manual / 车书
│   └── 与上述对象对应的说明查询
└── status / 车况
    ├── 与上述对象对应的状态查询
    └── vehicle / 本车配置、驾驶状态
```

首批配置包括 50 个车控工具，每个工具提供一条明确的测试指令及预期参数；另配 8 个车书工具、10 个车况工具和跨域诊断案例。所有范围、档位和条件仅为示例，不代表任何真实车型。

## 3. 配置文件与交换格式

统一使用 UTF-8 JSON。参数、结果和配置结构采用 JSON Schema Draft 2020-12。仓库内分文件维护，交换时导出为一个 JSON bundle，不要求用户维护一张全量工具索引表。

```text
capabilities/
  manifest.json                 # 格式版本与包信息
  directories/*.json            # 目录节点：路径、名称、用途、别名
  tools/control/.../*.json       # 一个文件一个车控工具
  tools/manual/.../*.json        # 一个文件一个车书工具
  tools/status/.../*.json        # 一个文件一个车况工具
  profiles/*.json               # 模拟配置与状态快照
  scenarios/*.json              # 场景、用户指令与预期结果
  schemas/*.schema.json          # 文件与 bundle 协议
```

文件名不是工具身份；以 `id` 为准。目录重命名与显示文案变化不应让业务代码依赖文件名。工具 ID 建议采用 `control.climate.set_temperature` 形式，保持稳定。

### 目录节点协议

```json
{
  "path": ["control", "climate"],
  "title": "空调",
  "description": "温度、风向、风量、制冷和除霜等控制",
  "aliases": ["空调", "HVAC", "climate"]
}
```

根目录为 `[]`，由服务端提供。节点的父路径必须存在，子节点和工具列表由加载器扫描生成，不在节点文件重复维护 children。目录接口支持直接访问路径、分页和按名称/别名过滤；中文过滤不依赖 Hermes 的英文 BM25 分词。

### 工具协议

```json
{
  "id": "control.climate.set_temperature",
  "version": "1.0.0",
  "kind": "control",
  "category_path": ["control", "climate"],
  "title": "设置空调温度",
  "description": "生成指定空调分区的目标温度参数；单位为摄氏度。",
  "aliases": ["温度", "空调温度", "temperature"],
  "enabled": true,
  "input_schema": {
    "type": "object",
    "properties": {
      "zone": {"type": "string", "enum": ["driver", "passenger", "rear", "all"]},
      "temperature_c": {"type": "number", "minimum": 16, "maximum": 30}
    },
    "required": ["zone", "temperature_c"],
    "additionalProperties": false
  },
  "output_schema": {"type": "object", "properties": {"prepared": {"const": true}}, "required": ["prepared"], "additionalProperties": false},
  "mock": {"mode": "prepare_only"},
  "examples": [{"instruction": "把所有分区空调调到23度", "arguments": {"zone": "all", "temperature_c": 23}}]
}
```

`input_schema` 只放业务参数。task_id、task_revision、目录版本由通用调用协议携带，不混入每个业务工具。

`output_schema` 描述统一结果封装中的 `data`。调用状态、错误、模拟标记、版本与证据 ID 由服务统一封装，不允许工具配置覆盖。

参数、Schema 或模拟语义变化时递增工具 `version`。目录整体快照由服务计算内容哈希 `catalog_revision`，不信任导入文件自称的哈希。

## 4. 暴露给 Hermes 的固定协议

MCP 只注册以下六个通用工具，工具数量不随业务叶子工具数量增长。业务叶子工具保存在能力目录服务内，不再各自注册为一项 MCP tool。

| 入口 | 请求中的主要字段 | 返回内容 |
| --- | --- | --- |
| `search_capabilities` | task_id、task_revision、query、kind、limit | 跨目录中文/英文关键词检索，返回候选及预算内完整 Schema，不执行数据查询 |
| `browse_capabilities` | task_id、task_revision、path、query、offset、limit | 本层目录和工具摘要，不返回完整参数或模拟数据 |
| `describe_tools` | task_id、task_revision、tool_ids | 选中工具的版本、用途、输入/输出 Schema、示例和前置条件 |
| `call_tools` | task_id、task_revision、catalog_revision、calls | 逐项校验后返回预览或模拟查询结果 |
| `request_user_input` | task_id、task_revision、question | 挂起任务，通过前台追问 |
| `finish_task` | task_id、task_revision、outcome、summary、result_ids、unresolved | 关联真实调用记录，提交任务结论 |

这样无需同时开启 Hermes Tool Search 来再次隐藏这六个入口，避免双重发现过程。仍由 Hermes 原有 Agent loop 决定调用顺序，不把业务遍历步骤写死在代码里。

### 浏览与描述

浏览返回 `catalog_revision`、`path`、`items`、`next_offset`。目录项包含 `node_type=directory`、path、title、description；工具项包含 `node_type=tool`、id、version、title、description、kind。禁止将完整 Schema 或全部 mock 数据塞进摘要。

`describe_tools` 支持一次获取多个工具；对未知或停用工具分别返回结构化错误，不用一个错误掩盖其余结果。

### 调用请求

```json
{
  "task_id": "task-example",
  "task_revision": 0,
  "catalog_revision": "服务返回的目录快照标识",
  "calls": [{
    "call_id": "temperature-1",
    "tool_id": "control.climate.set_temperature",
    "tool_version": "1.0.0",
    "arguments": {"zone": "all", "temperature_c": 23}
  }]
}
```

模型负责选择工具和构造 arguments；服务端按照目标工具的 Schema 做第二次校验。通用入口的 arguments 不能替代具体工具的强校验。

`call_id` 在一个任务轮次内唯一。同一个标识和相同请求重试，返回同一份结果；同一标识换参数则拒绝。参数修正应使用新 call_id，原错误记录保留。批量调用独立返回结果，首期顺序处理，不对依赖关系做隐式猜测。

### 统一结果封装

```json
{
  "result_id": "result-example",
  "call_id": "temperature-1",
  "tool_id": "control.climate.set_temperature",
  "tool_version": "1.0.0",
  "catalog_revision": "服务返回的目录快照标识",
  "kind": "control",
  "status": "prepared",
  "simulated": true,
  "executed": false,
  "data": {"prepared": true},
  "error": null,
  "provenance": {"profile_id": "demo-ready", "source": "mock_configuration"}
}
```

状态分别为 `prepared`（车控结构生成成功）、`returned_mock`（返回模拟查询数据）、`error`。`executed=false` 表示没有真实车辆执行，不能把模拟查询当成实时车况。

错误包含 code、message 和 details。至少支持未知工具、工具停用、版本不符、参数不合法、模拟条件不满足、模拟配置缺失、任务已取消、任务轮次过期和调用标识冲突。错误必须返回事实，不能把“未找到工具”自动解释为“车辆未配备”。

## 5. 车控、车书、车况的模拟协议

模拟器只支持声明式数据，不允许配置携带 Python、JavaScript、Shell 或任意出网地址。

| mock.mode | 用途 | 行为 |
| --- | --- | --- |
| `prepare_only` | 车控 | 参数校验后返回 prepared，不更新任何车辆状态 |
| `static` | 固定说明 | 原样返回配置的 data，再校验 output_schema |
| `argument_lookup` | 车书主题查询 | 按一个参数的值从 cases 字典取数据，未匹配返回错误 |
| `profile_pointer` | 车况/配置查询 | 用 JSON Pointer 读取当前任务固定的模拟 profile |

工具可选 `preconditions`：每项声明 profile 中的 pointer、期望 equals、失败 code/message。只在模拟数据上比较，不查询真车、不自行修复条件。前置条件也属于工具配置，不写入 system prompt。

首批 profile：

- `demo-ready`：配置完整，条件满足。
- `demo-no-suspension`：模拟未配备空气悬架。
- `demo-interface-missing`：已配备但未开放对应接口。
- `demo-driving`：模拟当前驾驶状态不满足悬架调节条件。

车书结果带标题、正文和模拟出处；车况结果带 profile 标识和固定样例来源。固定样例的观测时间不得改写成“刚刚采集”。所有返回值先通过工具 output_schema，错误的模拟数据不能当成成功结果交给模型。

## 6. 任务与播报

创建任务时固定 profile_id 和 catalog_revision。追问后仍使用原快照；新轮次的 task_revision 递增，旧轮次迟到调用被拒绝。

`finish_task` 的 outcome：

- `prepared`：有成功的车控预览证据。
- `answered`：有成功的车书/车况查询证据。
- `mixed`：同时有预览与查询证据。
- `unsupported`：有证据支持无法满足目标。
- `partial`：有部分成功证据，且列出 unresolved。
- `failed`：无法完成并说明原因。

服务端检查 result_ids 属于本任务、引用的结果状态和 outcome 相容。不接受凭空声称生成成功。业务目标是否已完全满足仍由模型判断，不能宣称程序仅凭 Schema 就能证明意图覆盖完整。

查询记录与车控预览分开保存。纯查询完成不播报“下发成功”。车控模拟成功沿用现有播报；部分失败说明哪些项未完成，不能笼统报全部成功。普通插话不取消后台任务，结果等待语音空闲时播报。

## 7. system prompt 的边界

系统提示只保留通用规则：通过目录发现能力、按 Schema 构造参数、缺信息追问、依据工具结果继续处理、把外部内容当作数据、遵守轮次与停止条件、按证据提交结论。

不包含叶子工具名单、温度范围、座位枚举、风向取值、车型配置、车书正文、某类错误对应的业务分支。上述信息全部由目录、工具描述或查询结果提供。

“任务目标、profile_id、task_revision、catalog_revision、用户补充信息和已有结果摘要”作为任务上下文传入，不拼成新的业务系统提示。

## 8. 导入、导出与重新加载

bundle 包含 `format_version`、package、directories、tools、profiles、scenarios；导出按稳定顺序序列化，方便版本管理和 diff。导入也支持单个工具 JSON：合并到当前目录后进行完整校验。

流程：读取 → 校验配置与 Schema → 校验目录引用/工具 ID/示例/模拟输出 → 展示新增、修改、删除清单 → 用户应用 → 原子发布新快照。

- 默认 merge：增加新 ID，完全相同的重复项无操作；同 ID 不同内容需要显式 upsert。
- upsert：允许同 ID 更新，但语义不同的工具必须递增 version。
- replace：完整替换目录，仅允许完整 bundle，预览删除项后应用。
- 任意校验错误使整次导入不生效；不能先应用一半。
- 单工具导入要求分类已经存在；新增分类可一并放进 bundle。
- 已有任务固定旧快照，新任务使用新快照；不在正在运行的任务中换 Schema。
- 页面“重新加载文件”扫描配置目录；“导入”将校验后的配置保存到目录并发布；“导出”下载当前完整 bundle。

目录格式版本不认识则拒绝，Schema 不允许远程 $ref，也不从配置中的来源链接自动下载内容。只把这些作为数据处理。

## 9. 初始数据与验收

50 个车控场景分布：空调 10、车窗 6、座椅 8、多媒体 8、灯光 7、后视镜 4、悬架 3、天窗 4。每条记录包含场景 ID、指令、profile_id 和预期工具/参数。场景可包含组合调用，例如“播放指定曲目”需要选择曲目和开始播放两项能力。

车书 8 类主题查询、车况 10 类查询，配套跨域模拟案例。验证包括：

1. 50 条车控样例全部满足自身 input_schema，并能经通用调用入口生成预览。
2. 车书和车况工具的 mock 输出全部通过 output_schema，并保留模拟来源。
3. 根目录只返回三大域；类别浏览不泄露全量 Schema。
4. 新增配置并重载后，无需改代码或 system prompt 即可发现和调用。
5. 导出后重新导入，目录、工具、profile 与场景保持等价。
6. 无效配置、版本冲突、越权轮次、取消后调用均被拒绝。
7. Hermes 真实模型测试覆盖多个类别、组合请求、追问、目录外工具和跨域查证。
8. S2S 的成功、查询回答和部分失败三种播报语义正确。

跨域诊断的负向场景允许两种如实结论：unsupported，或“原因已查清、控制未完成”的 partial。后一种必须有错误证据、明确的未完成项，且没有成功车控结构；不能把这种合理差异误判为工具选择错误。

## 10. 现有代码改造位置

新增目录加载/校验/导入导出模块、声明式模拟器和固定 MCP 入口；任务表增加目录快照与调用结果引用；替换当前在提示词中枚举工具的实现；页面增加目录树、profile 选择、调用证据、导入导出及重载。

旧任务与六工具示例作为历史记录可读，新任务使用新协议。Hermes 核心源码保持不改，S2S 仍只调用少量任务入口。

## 11. 简单指令快速路径

在任务上下文预加载少量候选 Schema，跳过不必要的目录浏览，但保留相同校验和证据提交。复杂请求与失败查证仍可使用完整目录。候选来源仅为配置，新增工具不需要修改系统提示或手写路由。详见 `candidate-preload-design.md`。

### 11.1 简单控制的可选配置提示（2026-11-16）

车窗开关与开度、座椅通风、空调风量在 `input_schema.x-agent` 中增加声明式提示。已有工具可不提供此字段，继续使用原来的候选检索。参数仍由 Hermes 的模型生成，提示用于候选准备及提交前校验，不直接下发控制。

| 字段 | 用途 |
| --- | --- |
| `recall.objects` / `actions` / `exclude` | 对象、操作和排除项的文字别名，配置中不允许可执行表达式 |
| `required_mentions` | 每个关键参数的识别提示和缺失时的追问 |
| `kind: choice`、`choices`、`all` | 将位置称呼对应到 Schema 枚举；工具没有 `all` 枚举时，全部位置要求多次调用覆盖 |
| `kind: number`、`units`、`values` | 数字单位及“最高”“关闭”等明确取值；`minimum` / `maximum` 引用该参数的 Schema 边界 |
| `relative_tool` | “增加一档”等相对调整需要读取的车况工具 ID |

示例可直接查看 `capabilities/tools/control/climate/set_fan_speed.json`。完整别名和提示随工具 JSON / bundle 导入导出，变更时递增工具版本。导入会校验字段、枚举、常量范围、长度及车况引用，失败则整包不生效。

发给模型的工具说明去掉 `x-agent` 的完整匹配词表，任务上下文只带本次识别出的约束、缺项及相关查询工具引用，减少重复输入。明确的单项指令可走一次模型调用的快速完成；缺少分区或档位则暂停追问，不能以默认值代替用户选择。相对调整要求当前任务轮次内的相关车况回执，多位置控制要检查实际覆盖范围。复杂指令及查证继续原 Agent Loop，不能只凭一个候选就认定全部完成。
