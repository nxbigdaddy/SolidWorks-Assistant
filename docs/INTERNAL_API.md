# 内部接口与事件

## 1. 计划对象

`Plan` 包含 `id/task/created_at/steps`；`ToolCall` 包含 `id/tool/arguments/description`。任何模型字段都先
经过领域解析，再由 `ToolRegistry` 校验。未知工具、未知参数、非有限数值、超范围尺寸和空必填值全部
在进入 COM 之前拒绝。

公开建模工具为：`session.ensure`、`document.new_part`、`document.open_readonly`、
`selection.plane`、`sketch.begin`、`sketch.circle`、`sketch.rectangle`、`sketch.line`、
`sketch.centerline`、`feature.extrude`、`feature.revolve`、
`model.inspect`。`library.inspect_document` 与 `library.modify_part_copy` 是应用内部命令，不提供给模型。
`assembly.pack_and_go` 与 `document.export` 同样是受应用服务约束的内部命令。

## 2. COM IPC

主进程向单个 spawn 工作进程发送：

```json
{"kind":"call","id":"request-id","call":{"tool":"model.inspect","arguments":{}}}
```

工作进程返回 `StepResult`，包含 `ok/message/error_code/data/before/after/duration_ms`。COM 对象、异常对象、
文件句柄和凭据不得跨进程。调用串行化；超时或用户停止会终止助手进程并丢弃该进程的队列。

## 3. 控制器事件

| 事件族 | 主要事件 |
| --- | --- |
| 规划 | `planning_started/progress/clarification/succeeded/failed` |
| 执行 | `step_started/succeeded/failed`、`plan_completed/aborted` |
| 恢复 | `retry_ready`、`failed_step_replaced`、`remaining_replaced`、`manual_*` |
| 模型修复 | `repair_started/progress/failed` |
| 模型库 | `library_started/progress/finished/failed` |
| 派生 | `derivation_started/succeeded/failed` |
| 分析 | `analysis_started/succeeded/cancelled/failed` |
| 实时上下文 | `solidworks_event`、`solidworks_context`、`solidworks_capabilities` |
| 持久批任务 | `jobs_started/changed/finished/failed`、`all_cancel_requested` |
| 操作学习 | `demonstration_started/action/finished/approved`、`demonstration_replay_ready/recorded` |
| 能力实验 | `analysis_*`（`kind=capability_lab`） |
| 迁移 | `legacy_migration_started/completed/failed` |

Qt 信号传递跨线程结果。UI 不直接调用 COM，也不在后台线程操作控件。

## 4. 高级适配器协议

`advanced` 中的产品族、装配体、工程图、PMI 和 PDM 服务都只依赖协议化适配器。
每个适配器先返回 `CapabilityStatus`，再接收经验证的不可变请求对象和取消事件。返回结果仅包含 JSON 可序列化证据，不得泄漏 COM 引用。

写模型请求在发送 Worker 前要验证文档扩展名、源/目标哈希、配置名、持久引用、数值范围和输出冲突。
PDM/Document Manager 协议默认只读；引导回放默认不执行写操作。

## 5. Manifest schema 2

顶层字段包括路径、标题、文档类型、配置、特征、尺寸、文档属性、配置属性、材料、方程、引用、工程图、
几何摘要和采集时间。`display_value_mm` 只用于被识别为长度的尺寸；原始 `system_value` 同时保留。参数哈希
基于按键排序、紧凑 UTF-8 JSON，并排除 `captured_at/path/title` 等采集元数据；完整 Manifest 仍原样保存。
增量跳过以完整文件 SHA-256 为准；快速指纹只用于大批量发现。参数读取前后再次核对完整哈希，避免把
读取期间发生变化的文件与 Manifest 错配。

## 6. 扩展规则

新增工具时必须同时提交 schema、COM 实现、后验条件、Fake 测试、错误恢复说明和真实夹具验收项。
新增内部命令不能进入模型工具描述。改变 Manifest 字段要提升 schema 版本，并提供旧快照兼容策略。
