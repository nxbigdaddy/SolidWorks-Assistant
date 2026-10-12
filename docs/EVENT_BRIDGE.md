# C# Add-in 事件桥与版本能力矩阵

## 1. 定位

事件桥是运行在 SolidWorks 进程内的只读侧车，不替代 Python STA COM Worker。Add-in 负责接收应用级
事件、提取轻量文档快照和探测当前 Interop 能力；Python Worker 仍然是唯一执行建模命令的组件。
任何 COM 对象都不得跨进程传递。

## 2. 数据通道

Add-in 在 SolidWorks 回调线程中只构造普通字典并入队。后台分发线程把 UTF-8 JSON 写入：

- `%LOCALAPPDATA%\SolidWorksAIAssistant\event-bridge\events.jsonl`
- `%LOCALAPPDATA%\SolidWorksAIAssistant\event-bridge\capabilities.json`
- `%LOCALAPPDATA%\SolidWorksAIAssistant\event-bridge\diagnostics.log`

同时尝试写入命名管道 `SolidWorksAssistant.EventBridge.v1`；没有监听器时会快速降级到 JSONL，不能阻塞
SolidWorks。事件文件达到 10 MB 后保留一个 `.1` 轮转文件。Python `EventBridgeMonitor` 从当前文件末尾
开始读取，验证 schema、处理不完整行和轮转，并按 `event_id` 去重。

## 3. 事件协议

事件 envelope 固定包含 `schema_version`、`sequence`、`session_id`、`event_id`、`timestamp_utc`、
`process_id`、`source`、`name` 和 `payload`。当前事件包括：

- `bridge.connected`、`bridge.heartbeat`、`bridge.disconnecting`
- `document.created`、`document.opened`、`document.loaded`、`document.closed`
- `document.active_changed`
- `document.events_attached`、`document.selection_changed`
- `document.regenerated`、`document.saved`、`document.destroyed`
- `context.changed`（包含文档、配置、保存/忙状态和选择持久引用）
- `command.started`、`command.finished`
- `solidworks.destroying`

事件只能作为状态变化提示。执行器收到事件后仍需重新采集快照，不能把事件本身当成建模成功证据。

## 4. 能力矩阵

能力矩阵按当前加载的 `SolidWorks.Interop.sldworks` 反射实际类型和成员，不声称未测试版本兼容。当前探测
Task Pane、Add-in 回调、应用/文档事件、选择上下文、持久引用、`SaveAs3`、Pack and Go、产品族配置、
装配配合/干涉、工程图/BOM、PMI、草图、拉伸和旋转等 25 项能力。`release_year_is_derived` 表示年份由主修订号推导，正式发布矩阵仍需逐版本夹具回归。

## 5. 构建与注册

开发机要求 Visual Studio 2022 MSBuild、.NET Framework 4.8 Reference Assemblies、64 位 SolidWorks 和
对应版本的 API redist：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build_event_bridge.ps1
```

注册和取消注册需要管理员 PowerShell：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\register_event_bridge.ps1 -Action Register
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\register_event_bridge.ps1 -Action Unregister
```

运行时验收：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\check_event_bridge.ps1
```

正式安装包必须为程序集配置受控的强名称签名，避免使用未签名 DLL 的 `/codebase` 警告。开发构建目前未
内置私人签名密钥。

## 6. 本机验收基线

2026-08-10 在 SolidWorks Premium 2025 SP5.0、修订号 `33.5.0`、Interop `33.5.0.53` 上验证：

- SolidWorks 自动调用 `ConnectToSW`；
- `SetAddinCallbackInfo2` 成功，Add-in 产生 `bridge.connected`；
- `OnIdleNotify` 连续产生心跳；
- Task Pane 创建成功，可显示活动文档、配置、选择数和桌面端入口；
- 文档级事件宿主读取了零件与“前视基准面”选择的实时上下文；
- 25/25 项反射能力可用；
- 成功连接后诊断日志没有新增异常；
- Python 事件读取、去重、不完整行和后台停止测试通过。

当前机器只安装了 SolidWorks 2025 SP5.0。2024/2026 的加载、高 DPI、关闭/重载和完整文档生命周期仍应由多版本能力实验室逐机执行，不能由单一版本验证替代。
