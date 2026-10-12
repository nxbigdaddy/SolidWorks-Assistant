# v4.2.0 Pi 持久恢复 Agent 验证报告

验证日期：2026-09-03（Asia/Shanghai）<br>
工程：`<workspace>/solidworks_tools_agent`<br>
版本：`4.2.0`

## 1. 本轮结论

Pi 已作为执行失败恢复后端接入，而不是直接控制 SolidWorks。任务级会话、同会话校验反馈、轮次预算、
自动回退、设置与界面状态均已有源码和自动测试。Python 的 `SupervisorPolicy`、`ToolRegistry`、COM
串行执行器、后验验证和撤销仍是最终授权边界。

旧工程 `<workspace>/solidworks_tools` 未修改。发行包不含 01yx DLL，也不复制 Pi GUI、
`<workspace>/pi_agent` 或用户全局 Pi 配置。Pi 缺失时默认自动后端会继续使用内置 PydanticAI 监督器。

## 2. 自动化质量门禁

最终一次完整运行结果：

| 门禁 | 结果 |
| --- | --- |
| `ruff check launcher.py src scripts tests` | 通过 |
| `mypy src` | 通过，67 个源码文件 |
| `compileall` | 通过 |
| `pytest -q` | 245 项通过，0 失败 |
| Event Bridge Release | 0 警告，0 错误 |
| Event Bridge 自包含测试 | 8/8 通过 |

Pi 新增覆盖包括：

- 无效 JSON 后继续使用同一客户端会话修正；
- 重复完全相同失败调用被本地策略拒绝后继续修正；
- 多次执行失败之间复用同一服务和任务预算；
- 预算耗尽只返回 `request_review`，不产生可执行计划；
- 显式与系统 Pi 运行时发现；
- API Key 不进入命令行，Pi 启动参数包含全部禁用开关；
- Python 假子进程验证 JSONL 接受、settled 和最终文本协议；
- 真实 Pi CLI 对本机 OpenAI 兼容 SSE 假服务完成恢复，HTTP 请求没有 `tools` 字段。

最后一项只发送合成失败上下文与工具 Schema到 `127.0.0.1`，没有连接第三方模型，没有发送真实日志、
CAD 数据、文件路径或凭据。

## 3. Pi 运行边界验证

本机发现并验证的组合：

- Node.js `25.2.1`；
- `@earendil-works/pi-coding-agent` `0.84.4`；
- RPC `prompt -> agent_settled -> get_last_assistant_text`；
- 自定义 provider `solidworks-assistant`；
- thinking 为 `off`；
- 请求工具为空。

启动命令固定包含 `--no-tools`、`--no-extensions`、唯一显式 provider、`--no-skills`、
`--no-prompt-templates`、`--no-themes`、`--no-context-files` 和 `--no-approve`。Pi 使用应用隔离的
`PI_CODING_AGENT_DIR`；子进程不会读取用户全局 Pi 扩展、技能、上下文或凭据。

## 4. 发布和冷启动

输出目录：`artifacts/release/4.2.0`

Setup 与 portable ZIP 的最终 SHA-256 以产物旁的 `SHA256SUMS.txt` 为准。验证报告本身会进入 ZIP，
因此不在随包文档中写入会形成自引用或重建漂移的产物哈希。EXE、Event Bridge 和 provider 可按需用
`Get-FileHash -Algorithm SHA256` 现场复核。

EXE 的 FileVersion 和 ProductVersion 均为 `4.2.0`。Setup Authenticode 状态为 `NotSigned`，因此不能
宣称已签名。

正式 `dist` 使用独立数据目录隐藏启动 8 秒：进程未退出、`Responding=True`、主窗口句柄非零、标题为
“SolidWorks 智能建模助手”，随后正常关闭且退出码为 0。

安装载荷被部署到工作区内唯一隔离目录，内部版本为 4.2.0，Pi provider 存在。该目录同样冷启动
8 秒通过并退出码 0；随后使用随包卸载器删除，目标目录确认不存在。该测试没有修改现有正式安装，
没有注册 Event Bridge、快捷方式或卸载注册表项。

## 5. 发布内容与第三方边界

正式发行目录统计：

- 392 个文件；
- 106 个 DLL；
- Event Bridge 存在；
- 数据库、JSONL、CAD、PDB、API Key/secret/credential 文件命中 0；
- `01yx`、`OneYx`、`pi-gui` 名称命中 0；
- Python 可执行源码中 `01yx`、`OneYx`、Python.NET/CLR 引用命中 0。

另对 `C:\Users\<user>\AppData\Local\Programs\01yx` 下 6 个 DLL 与发行目录 106 个 DLL
逐一计算 SHA-256：哈希重合为 0。

Pi 为 MIT 许可开源项目，但 4.2.0 不重新分发 Pi/Node 二进制，仅提供本项目静态 provider 和发现
适配层。这样不会把另一工程或用户全局 npm 内容混入发布包；目标机没有 Pi 时会安全回退。

## 6. 尚未宣称通过的边界

- 本轮没有把真实 SolidWorks 故障上下文发送到外部模型；真实建议质量仍需单独的数据传输授权。
- 本轮没有新增真实 SolidWorks 写操作；既有 2025 SP5.0 建模稳定性证据继续有效，但不能外推到
  2024/2026 或不同模板、语言、第三方 Add-in 组合。
- 4.2.0 未内嵌 Pi/Node 二进制。目标机要使用 Pi 模式，需要 Node 22.19+ 与经验证的 Pi 0.84.4；
  否则应使用默认自动回退或显式选择内置监督器。
- 安装器没有商业代码签名证书。
