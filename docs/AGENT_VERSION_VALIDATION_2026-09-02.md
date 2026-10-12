# SolidWorks 智能建模助手 v4.0.0 Agent 版验证报告

验证日期：2026-09-02（Asia/Shanghai）

## 1. 结论

v4.0.0 已在独立目录 `<workspace>/solidworks_tools_agent` 完成源码门禁、Agent 协议、PyInstaller、
Event Bridge、安装包和一次真实 SolidWorks 未保存零件冒烟。旧目录 `<workspace>/solidworks_tools` 未
作为本次写入目标。

验证支持以下结论：

- 监督 Agent 已接入失败恢复路径，但不直接持有 SolidWorks、Shell、文件或 DLL 执行能力；
- 模型建议必须经过本地恢复门禁和既有 ToolRegistry 全计划校验；
- 关闭 Agent 时仍能回退到旧的受限 Planner 修复；
- 源码、发布目录和便携 ZIP 不包含 01yx/OneYx/Python.NET/CLR 运行时；
- 发布应用和安装器可重建，应用可离屏冷启动；
- 未把真实日志、CAD 上下文或故障信息发送到外部模型服务。

## 2. 干净环境

使用项目自有 `.venv`，Python 3.12.8，且没有启用 `--system-site-packages`。关键依赖：

- `pydantic-ai-slim[openai] == 2.37.0`；
- `openai == 3.6.0`；
- `PyInstaller == 6.22.2`；
- `PySide6 == 6.11.2`；
- `Ruff == 0.16.5`；
- `mypy == 2.3.1`。

曾建立的带系统包虚拟环境已从项目目录移到
`%LOCALAPPDATA%\Temp\solidworks_tools_agent-system-venv-20260901`，不参与源码、构建或发布。

Ruff 新版在本机解析出的默认规则与旧环境不同；`pyproject.toml` 已显式固定为旧项目使用的
`E4/E7/E9/F` 门禁，避免质量规则随工具版本隐式变化。

## 3. Python 门禁

| 项目 | 结果 |
| --- | --- |
| `ruff check .` | 通过 |
| `mypy src launcher.py` | 62 个源码文件，0 问题 |
| `compileall -q src launcher.py` | 通过 |
| `pytest -q` | 215 项通过，22.52 秒；发布脚本复跑 215 项通过，21.26 秒 |

旧代码复制后的独立基线为 204 项通过；本版本增加或调整后的最终总数为 215。

## 4. Agent 专项验证

`tests/test_agent_supervisor.py` 覆盖：

1. PydanticAI `TestModel` 完成必要只读检查并产生可验证替代计划；
2. `stop` 和 `request_review` 不携带可执行步骤；
3. 模型已经变化或状态未知时拒绝替代；
4. 拒绝完全相同的当前/历史失败调用；
5. 拒绝非有限 JSON 数值；
6. 本地路径和凭据字段脱敏；
7. 超过 200 KB 的上下文在发送前拒绝；
8. OpenAI 兼容 base URL 规范化；
9. 本地 HTTP 假服务完成三轮协议：检查失败、检查工具、结构化最终结果。

本地 HTTP 测试核对请求路径 `/v1/chat/completions`、Bearer 认证头、三个只读工具和最终输出工具，
没有访问互联网。运行时测试还核对了 Agent 开关和最大模型请求数正确传入 `SupervisorService`。

## 5. PyInstaller 和应用冷启动

`build.ps1` 在干净虚拟环境中完成 Ruff、mypy、compileall、215 项 pytest 和 PyInstaller one-folder
构建。最终 EXE 的 FileVersion 与 ProductVersion 均为 `4.0.0`。

使用 `QT_QPA_PLATFORM=offscreen` 启动最终 EXE；6 秒后进程仍正常运行，然后按精确 PID 清理。

- EXE：`dist/SolidWorksAssistant/SolidWorksAssistant.exe`
- SHA-256：`8D328D9A6096207EC6C31BE2BB3DD5EA1D489F230C0919002E6AC22834466D62`

PyInstaller 警告文件包含未安装的可选 Provider、Realtime、Web UI 和 CLI 依赖提示；本版本实际使用
的 OpenAI Chat Completions 路径已由本地 HTTP 协议测试和最终 EXE 冷启动覆盖。构建分析没有出现
Python.NET 或 CLR hook。

## 6. Event Bridge

项目自有的 `.NET Framework 4.8 x64` Event Bridge 重新构建：

- MSBuild：0 警告，0 错误；
- 自包含测试：8/8 通过；
- SHA-256：
  `61FF6BE578E83A243E0552392A69766DBCA98DC77DBE574E86A59676D29E6AD0`。

该 DLL 位于 `artifacts/dotnet/EventBridge/Release/SolidWorksAssistant.EventBridge.dll`，由本项目
源码构建，只引用 Microsoft .NET 与已安装 SolidWorks 官方 Interop，不属于 01yx。

## 7. 01yx / DLL 独立性审计

最终发布目录包含 374 个文件、107 个 DLL。对当前机器
`C:\Users\<user>\AppData\Local\Programs\01yx` 下发现的 6 个 DLL 逐个计算 SHA-256，与
发布目录全部 DLL 比对，匹配数为 0。

同时验证：

- 发布路径名中 `01yx|OneYx|pythonnet|clr` 命中 0；
- 便携 ZIP 共 378 个条目，同一禁用名称集合命中 0；
- `src/sw_assistant`、`src-dotnet`、启动器、依赖清单和 spec 中没有 Python.NET、`import clr`、
  `AddReference` 或 `LoadLibrary` 运行时代码；
- 文档和生成的包元数据可以出现“01yx 独立性”这一说明文字，它不是库引用或运行时依赖。

## 8. 真实 SolidWorks 冒烟

2026-09-01 从 Agent 新目录运行 `hex-key-corner` 场景，只创建脚本自己的未保存零件：

- 8 个步骤全部完成；
- 单一实体，活动草图实体数为 0；
- `feature_count = 22`；
- `body_count = 1`；
- `volume_m3 = 3.6997510955267993e-06`；
- 两个拉伸的 `CheckFeatureUse` 均为 0；
- 两个拉伸的 `GetErrorCode2` 均为 0。

证据文件：`artifacts/live-agent-hex-key-corner-2026-09-01.json`

SHA-256：`AE114BEC113338DA433BAD37228CDC2C77E09A8F3A77E8BCA3B1DF4A430BF1A1`

该冒烟证明 v4 新依赖没有破坏既有 SolidWorks Gateway；它不是“真实云 Agent 修复一次 API 故障”
的替代证据。

## 9. 4.0.0 发布物

目录：`artifacts/release/4.0.0`

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `SolidWorksAssistant-4.0.0-Setup.exe` | 82,188,800 | `AEE6FAC3BA9CA18DE0A6BD4588FD7D57BB25C784945C7B72AB838E7BCD61BEF7` |
| `SolidWorksAssistant-4.0.0-portable.zip` | 83,023,284 | `EEA25A6317D0A6D82E872BF226F9375ED4E287D7D56EE0394D239C4985B9C419` |

`release-manifest.json` 记录版本 4.0.0、发布文件数 374、总字节 195,748,860、包含 Event Bridge、
禁用文件计数 0。安装器未使用商业 Authenticode 证书，状态为 `NotSigned`；分发时必须同时提供
`SHA256SUMS.txt`，不能把未签名状态描述为已签名发布。

## 10. 未完成或不能伪装成通过的边界

- 没有对外部模型端点做真实故障恢复；需要用户明确授权具体域名和允许发送的数据后再测；
- 安装器已构建并做内容/哈希审计，但本轮没有写入 Program Files 做隔离安装/卸载回归；
- 当前真实 CAD 覆盖仍是 SolidWorks 2025 SP5.0；其他主版本未验收；
- 真实复杂生产模型上的 Agent 建议正确率、限流、断网和长时间稳定性仍需试点统计；
- 本地策略能够阻止越权和已知危险恢复，但不能保证模型对未知几何问题总能给出有效方案。
