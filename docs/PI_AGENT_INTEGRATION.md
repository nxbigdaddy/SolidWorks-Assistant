# v4.2 Pi 持久恢复 Agent 集成

本文记录 `<workspace>/solidworks_tools_agent` 中 Pi 的实际集成方式、运行边界和部署条件。旧项目
`<workspace>/solidworks_tools` 未修改；本版本不引用 01yx 的 DLL、配置、提示词或运行时。

## 1. 集成目标

原 PydanticAI 监督器能够对一次失败做有限的结构化判断，但每次执行失败都会创建新的监督实例，
无法保留“上一候选为什么被拒绝、已经尝试过什么”的会话记忆。Pi 在本版本中只承担任务级持续推理：

- 同一计划的连续执行失败复用同一个 Pi RPC 进程和同一个持久 session id；
- Python 本地策略拒绝候选方案后，把拒绝原因回送给同一会话继续修正；
- 每轮收到最新的脱敏快照、失败步骤、成功前缀和历史尝试；
- 达到任务预算、状态未知或没有安全方案时转人工检查。

Pi 不接管 SolidWorks。全部 COM 写入、重建、特征读回、几何后验、撤销和暂停仍由 Python 执行层负责。

## 2. 实际数据流

```text
SolidWorks 单步失败
  -> Python 恢复分类和状态一致性检查
  -> 脱敏上下文（最大 200 KB）
  -> 同一任务的 Pi JSONL RPC 会话（无工具）
  -> SupervisorDecision JSON 候选
  -> Pydantic 严格 Schema
  -> SupervisorPolicy + ToolRegistry 全计划校验
       -> 拒绝：原因回送同一 Pi 会话，继续下一轮
       -> 通过：只把白名单 ToolCall 后缀交给原执行器
  -> 单步 COM + 快照/定义读回/后验
       -> 再失败：带新证据回到同一 Pi 会话
```

初始自然语言规划仍使用现有 Requirements Agent。Pi 专注于最需要跨轮记忆的“执行失败恢复”，避免在
同一版本同时替换规划和执行两条核心链路。

## 3. 进程与协议实现

`agent/pi_client.py` 使用 `subprocess.Popen` 直接启动 Node 和 Pi CLI，不经过 Shell。stdin/stdout
采用严格 JSONL；客户端等待 `prompt` 被接受和 `agent_settled`，再通过
`get_last_assistant_text` 读取完整结果。一次只允许一个请求进入会话，超时会终止子进程。

任务 session id 由计划 id 的 SHA-256 派生，不包含需求文本、文件名或路径。会话位于：

```text
%LOCALAPPDATA%\SolidWorksAIAssistant\pi-agent\task-<hash>\sessions
```

应用重启后重新加载同一计划时，Pi 可以从这个 session id 恢复历史。新计划、重置、停止、完成或设置
变化会关闭当前进程。

## 4. 强制安全边界

Pi 启动时固定使用：

- `--no-tools`：关闭内置和扩展工具；
- `--no-extensions --extension provider.mjs`：禁止扩展发现，只加载本项目的静态模型提供器；
- `--no-skills --no-prompt-templates --no-themes`：禁止发现用户 Pi 资源；
- `--no-context-files --no-approve`：不读取工程 AGENTS/CLAUDE 文件，不信任项目文件；
- `--thinking off`：兼容不允许 thinking 与严格结构输出组合的端点；
- 独立 `PI_CODING_AGENT_DIR`：不读取用户全局 Pi 配置、扩展和凭据。

API Key 不在命令行、提示词、session id 或审计事件中，只通过子进程环境变量传给静态 provider；
启动环境还会剔除继承的其他 `API_KEY`、`TOKEN`、`SECRET`、`PASSWORD` 和 `AUTHORIZATION` 变量。
发送给模型的值沿用项目统一脱敏器，路径和常见凭据会被替换，序列化上下文超过 200 KB 时拒绝发送。

即使模型输出合法 JSON，也必须满足以下本地硬条件：模型状态已知、没有发生非预期变化、恢复分类明确
允许 AI 修复、没有重复完全相同的失败调用、成功前缀与候选后缀整体通过 `ToolRegistry`。置信度不能
绕过这些规则。

## 5. 后端选择与故障降级

设置提供三种模式：

| 模式 | 行为 |
| --- | --- |
| 自动 | 优先 Pi；Pi 缺失、协议错误或启动失败时，本任务只回退一次到内置 PydanticAI 监督器 |
| Pi 持久多轮 | 强制 Pi；不可用时明确停止并显示错误，不静默换后端 |
| 内置 PydanticAI | 保留 v4.1 的单次监督路径 |

Pi 默认预算为每个任务 6 轮，可设置为 2–12。预算同时覆盖 JSON 修正、本地策略拒绝后的修正和后续
执行失败；用完后只能请求人工检查，不再生成可执行步骤。

## 6. 运行时发现和部署

发现顺序为：

1. 正式包旁的 `PiEngine/node.exe` 与 `PiEngine/pi-coding-agent/dist/bundle/cli.js`（为以后审计后内嵌预留）；
2. 管理员显式设置的 `SW_ASSISTANT_NODE` 与 `SW_ASSISTANT_PI_CLI`；
3. 系统 Node 与全局安装的 `@earendil-works/pi-coding-agent`；
4. 系统 Node 与上游 `@mariozechner/pi-coding-agent`。

当前 4.2.0 发布包只打包本项目的 `provider.mjs`，不复制 Pi GUI、另一工程的运行时或全局凭据。
本机验证组合为 Node 25.2.1、`@earendil-works/pi-coding-agent` 0.84.4；目标机建议固定安装：

```powershell
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.84.4
```

Pi 是 MIT 许可的开源项目。上游与协议文档：

- <https://github.com/earendil-works/pi>
- <https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md>
- <https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md>

## 7. 验证范围

自动测试覆盖 JSON/Markdown JSON 解析、无效输出的同会话修正、重复失败调用被拒后的修正、跨失败预算、
预算耗尽转人工、运行时发现、命令行无密钥、禁用工具参数和 JSONL 协议。另有一个真实 Pi CLI + 本机
假 OpenAI SSE 服务的端到端测试，确认模型请求没有工具字段，且候选计划仍经过 Python 授权。

该本机端到端测试没有连接第三方模型，没有发送日志、CAD 数据或本地文件路径。真实模型对真实复杂
故障的建议质量，以及 SolidWorks 2024/2026 和生产级拓扑组合，仍需在新建未保存夹具上单独验收。
