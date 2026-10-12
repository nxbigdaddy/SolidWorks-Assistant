# 受约束 SolidWorks 监督 Agent 架构

本文描述执行失败后的监督层。v4.2 默认优先使用 Pi 持久恢复会话，并保留 v4.1 PydanticAI 监督器
作为明确可选的后备。初始需求规划链路见
[v4.1 全流程建模 Agent 架构](FULL_AGENT_ARCHITECTURE.md)，Pi 实现细节见
[v4.2 Pi 持久恢复 Agent 集成](PI_AGENT_INTEGRATION.md)。

## 1. 目标与非目标

本版本的目标不是让大模型直接“接管 SolidWorks”，而是在原执行器已经暂停后，利用模型归纳失败
上下文、读取允许的工具 Schema，并提出一个可审查的恢复决策。真正的执行权仍在本地确定性代码中。

Agent 主要改善以下问题：

- 失败原因组合复杂，固定错误码表无法覆盖上下文差异；
- 当前面、草图、选择、配置和拓扑已变化时，简单重试容易再次失败；
- 替代步骤需要同时参考成功前缀、失败历史、模型快照和工具约束；
- 新错误可以先安全停止或请求人工检查，而不是猜测并继续写模型。

本版本不承诺修复所有 SolidWorks API 错误，也不把模型输出当作成功证据。Agent 不负责初始 COM
连接、拓扑对象生命周期、特征定义读回、后验校验或回滚，这些继续由原稳定执行层负责。

## 2. 组件与数据流

```text
SolidWorks 单步执行失败
        |
        v
本地恢复分类器：状态是否可知、模型是否变化、是否允许 AI 修复
        |
        +---- 不允许 ----> 停止 / 人工处理
        |
        v
脱敏和 200 KB 上下文上限
        |
        v
Pi 持久恢复 Agent（工具全禁用，2–12 轮/任务）
        |  启动或协议失败且设置为自动
        +----------------------> PydanticAI 后备监督器（只读检查，2–8 次请求）
        |
        v
SupervisorDecision：replace_remaining / request_review / stop
        |
        v
本地 SupervisorPolicy：状态门禁、失败调用去重、ToolRegistry 全计划校验
        |
        +---- 拒绝 ----> 原因回送同一 Pi 会话继续修正；预算耗尽后人工处理
        |
        v
原执行器排队替换后缀 -> 单步 COM -> 快照和后验 -> 继续或再次停止
```

核心文件：

- `src/sw_assistant/agent/models.py`：严格结构化决策协议；未知字段一律拒绝。
- `src/sw_assistant/agent/supervisor.py`：Agent、只读工具、请求限额和模型 Provider。
- `src/sw_assistant/agent/pi_client.py`：隔离 Pi 进程、JSONL RPC、会话和超时。
- `src/sw_assistant/agent/pi_supervisor.py`：多轮提示、严格输出解析和策略反馈闭环。
- `src/sw_assistant/agent/pi_assets/provider.mjs`：唯一显式加载的静态模型提供器。
- `src/sw_assistant/agent/policy.py`：独立于模型的本地授权边界。
- `src/sw_assistant/runtime.py`：把授权后的计划接回既有恢复状态机。
- `src/sw_assistant/tools.py`：唯一的 SolidWorks 工具白名单和参数验证来源。

## 3. Agent 能看到什么

内置 PydanticAI 后备监督器必须先调用下面两个只读闭包工具，才能给出最终决策：

1. `inspect_failure`：脱敏后的失败步骤、成功前缀、前后模型快照、恢复判断和历史失败尝试。
2. `inspect_available_tools`：本地 `ToolRegistry` 生成的精确工具名称、字段类型、枚举和范围。

它还可以调用一次 `inspect_recovery_policy`，读取不可绕过的恢复规则。每次运行的总工具调用上限为
3；模型请求上限默认为 4，设置范围为 2–8；模型输出 token 上限为 8000。缺少必要检查时，即使
模型给出了格式正确的答案，本地也会把本次 Agent 运行判定为失败。

Pi 后端不注册这些工具，也没有任何其他工具。相同的脱敏失败上下文、完整工具 Schema、恢复规则和
`SupervisorDecision` JSON Schema 作为普通文本输入；Pi 只能返回候选 JSON。本地拒绝原因作为下一条
普通消息回送同一会话，不能由 Pi 直接调用工具或执行候选步骤。

发送前执行以下数据保护：

- 字段名包含 `api_key`、`authorization`、`password`、`secret` 或 `token` 时替换为脱敏值；
- 路径字段和文本中的本地盘符/UNC 路径替换为 `<local-path>`；
- 序列化后超过 200 KB 时拒绝发送；
- API Key 不进入提示词或审计载荷，只交给 HTTP Provider 生成认证头。

本版本使用 OpenAI Chat Completions 兼容接口；HTTP 客户端禁止自动跟随重定向，避免认证头被无意
带到另一个地址。阿里云 DashScope 兼容地址会显式关闭思考扩展，以保持结构化工具协议稳定。

## 4. Agent 不能做什么

两个监督后端都没有以下能力；Pi 进程还通过 `--no-tools` 在运行时关闭全部工具：

- Shell 或任意进程执行；
- 文件读取/写入工具；
- Python/JavaScript 动态代码执行；
- DLL、程序集或插件加载；
- 直接 COM、SolidWorks SelectionManager 或 FeatureManager 调用；
- 保存、覆盖、删除或导出用户文档；
- 修改工具白名单或本地授权规则。

因此它的“替代步骤”只是提案，不是函数调用授权。Agent 也不使用 01yx 的二进制、协议或运行时。

## 5. 唯一允许的输出

`SupervisorDecision` 只有三种动作：

| 动作 | 含义 | 可携带替代步骤 |
| --- | --- | --- |
| `replace_remaining` | 建议替换尚未执行的计划后缀 | 必须有 1–64 步及可验证后置条件 |
| `request_review` | 信息不足或需要人检查 | 否 |
| `stop` | 风险或状态不允许继续 | 否 |

输出还必须包含非空原因和 0–1 的置信度。置信度只用于诊断，不参与越权放行。

## 6. 本地授权规则

只有全部满足时，`replace_remaining` 才能变成可执行计划：

1. `recovery_assessment.state_known == true`；
2. `recovery_assessment.model_changed == false`；
3. `recovery_assessment.ai_repair_recommended == true`；
4. 每一步可以转换为有限 JSON `ToolCall`；
5. 没有重复当前或历史中“工具名 + 完全相同参数”的失败调用；
6. “已完成前缀 + 新后缀”整体通过 `ToolRegistry.validate_plan`；
7. 校验后的替代后缀非空。

这层策略不依赖模型自觉。模型即使忽略系统提示，也无法绕过 Python 中的硬门禁。

## 7. 与 01yx 的本质区别

本项目没有拆取或复用 01yx 函数库。可以借鉴的是工程方法：将自然语言意图、上下文推理、稳定工具
调用和结果验证分层。具体实现仍是本项目自有的 Pi/PydanticAI 适配层、pywin32 Worker、工具 Schema、
持久引用/几何签名解析和后验验证。

稳定性来自多层组合，而不是某个 DLL：

- Agent 负责处理“该怎么调整计划”的开放问题；
- 恢复分类器决定“当前是否允许让 AI 提建议”；
- 工具注册表决定“什么操作和参数合法”；
- COM Worker 决定“何时、在哪个文档/草图中执行”；
- 快照和特征读回决定“API 返回后是否真的成功”；
- 撤销和暂停状态机决定“失败后如何保留可恢复状态”。

## 8. 依赖与升级策略

Python 运行依赖固定为 `pydantic-ai-slim[openai]==2.37.0`。Pi 组合固定验证为 Node 22.19+ 与
`@earendil-works/pi-coding-agent==0.84.4`；发布包当前不复制外部 Pi 运行时，没有安装时自动模式会
回退。依赖升级必须重新执行 JSONL、结构化输出、无工具门禁、本地假端点、全量回归和 PyInstaller
冷启动，不得仅修改版本号后发布。

## 9. 仍需目标环境验证的事项

- 真实模型对本企业/项目错误日志的建议质量；
- 目标代理、TLS 检查、兼容端点对工具调用和结构化输出的支持；
- SolidWorks 其他主版本、语言、模板和第三方 Add-in 组合；
- 长时间运行时模型限流、超时和断网后的用户体验；
- 带真实生产拓扑复杂度的恢复建议命中率。

这些测试必须先使用脚本新建的未保存零件，不得直接用生产 CAD 文件做首次恢复实验。
