# 系统架构

## 1. 总体结构

```text
PySide6 UI
  ├─ ApplicationController
  ├─ PlannerService ── ModelClient / OfflinePlanner
  ├─ ExecutionEngine ── ToolRegistry ── SolidWorksWorkerClient
  ├─ LibraryService / LibraryAnalyzer ── LibraryRepository
  ├─ Advanced services ── health / family / assembly / drawing / learning / manufacturing / PDM / lab
  ├─ PersistentBatchRunner ── Job / JobItem lease store
  ├─ EventBridgeMonitor ── FreshContextStore
  ├─ Derivation / PackAndGo / Export services
  └─ Settings / History / Audit
                                │ JSON IPC
                                ▼
                     SolidWorks COM Worker Process

SolidWorks C# Add-in ── Task Pane / document events / context JSONL / capability snapshot

Optional read-only MCP server ── LibraryRepository / LibraryAnalyzer
```

`src/sw_assistant` 是唯一应用包。依赖方向从 UI 指向应用服务、再指向领域接口和基础设施；
领域模型不导入 Qt、win32com、SQLite 或网络模块。

## 2. 包职责

| 包 | 职责 |
| --- | --- |
| `domain` | 计划、工具调用、结果、快照、状态和错误类型 |
| `planning` | 需求预检、提示词、模型调用、离线规划和响应校验 |
| `execution` | 状态机、后验验证、失败指纹和恢复 |
| `solidworks` | JSON IPC、STA COM 工作进程、API 适配和单位换算 |
| `library` | 文件发现、哈希、参数快照、查询、分析、派生、Pack and Go 与导出任务 |
| `advanced` | 健康诊断、产品族、装配体、工程图、语义搜索、操作学习、PMI/DFM、PDM 和能力实验 |
| `automation` | 持久化任务、幂等任务项、租约/心跳、暂停/恢复/取消与崩溃恢复 |
| `integration` | 可选的只读 MCP 适配层 |
| `infrastructure` | 设置、DPAPI、SQLite、审计和历史 |
| `ui` | 主窗口、页面、对话框、Qt 信号桥和主题 |

## 3. 关键边界

### 3.1 模型边界

模型只接收工具 schema 和任务上下文，只返回 `Plan`。模型不能访问 COM 对象、文件句柄、
数据库连接或 API Key。服务端文本进入 UI 前按纯文本处理。

### 3.2 COM 边界

所有 COM 对象只存在于专用工作进程的单一 STA 线程。主进程只发送 JSON 可序列化命令。
工作进程失联时，未完成步骤失败，已完成步骤保留；用户确认后可以建立新工作进程继续。

### 3.3 数据边界

设置、库、历史和审计位于 `%LOCALAPPDATA%\SolidWorksAIAssistant`。工程目录只保存代码、
文档和测试。数据库连接不跨线程共享；仓储为每个线程缓存一个连接，启用 WAL 和
`synchronous=NORMAL`，线程结束时显式关闭。

## 4. 可靠性策略

- 每个异步请求有递增请求号；迟到结果被丢弃。
- 执行状态只有 `idle/ready/running/paused/completed/aborted`。
- 工具调用在执行前 schema 校验，在执行后快照校验。
- 相同失败指纹不能无限原样重试。
- 日志为追加写 JSONL；SQLite 记录查询友好的摘要。
- 文件写任务记录 `planned/staging/verifying/committing/succeeded/failed`。

## 5. 扩展方式

新 SolidWorks 动作必须同时增加工具 schema、COM 实现、后验条件、纯测试和真实夹具。
新模型供应商通过兼容客户端或独立适配器接入，不得在 UI 中散布供应商判断。
