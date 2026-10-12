# v4.1 全流程建模 Agent 架构

## 1. 目标

v4.1 将 v4 只在执行失败后工作的监督 Agent，扩展为覆盖“需求理解 → 计划生成 → 本地校验反馈 →
计划预览 → 单步执行 → 失败恢复”的完整受约束 Agent 链路。

这里的“发挥 Agent 能力”指让模型处理开放性的意图理解、合理假设、能力降级和计划纠错，不是把
SolidWorks、电脑或文件系统的控制权交给模型。全部 CAD 写入仍只能通过本地工具白名单、串行 COM
Worker 和后验验证。

## 2. 完整流程

```text
用户自然语言
  |
  +--> ISO 4762 等本地标准件可确定解析 --> 本地计划
  |
  v
需求规划 Agent（最多 2–8 次请求，默认 6）
  |-- inspect_user_request：脱敏需求 + 非敏感实时上下文
  |-- inspect_available_tools：精确 JSON Schema 和写入范围
  |-- inspect_planning_policy：假设、澄清、降级和禁用能力
  |
  v
PlanningDecision
  |-- create_plan
  |-- request_clarification
  `-- unsupported
  |
  v
输出校验器 + PlanningAgentPolicy
  |-- ToolCall 有限 JSON
  |-- ToolRegistry 参数/枚举/范围/顺序
  |-- 推断尺寸必须公开写入 assumptions
  `-- 失败信息通过 ModelRetry 返回同一 Agent 会话纠错
  |
  v
计划预览：步骤 + Agent 假设
  |
  v
原执行器逐步写 SolidWorks、采集快照和读回特征
  |
  +--> 成功 --> 下一步
  `--> 失败 --> 恢复分类 --> 监督 Agent --> 本地二次授权 --> 替换后缀/人工/停止
```

## 3. 需求规划 Agent 的能力

需求规划 Agent 可以：

- 先检查当前真实工具能力，再选择可执行的建模路径；
- 对普通、非配合、非制造关键尺寸采用合理近似；
- 对品牌外形采用“近似包络”，但不得宣称为官方精确规格；
- 当细节工具不足时生成最有用的简化模型，并公开省略内容；
- 把全部推断尺寸、近似值和省略特征写入 `Plan.assumptions`；
- 在同一会话内收到本地参数/枚举/顺序拒绝原因并重新生成；
- 读取不含标题、路径和持久引用的实时 SolidWorks 摘要，例如文档类型、只读状态、选择类型和能力；
- 对精密配合、接口、制造、安全或用户明确要求的精确规格，一次性提出最多三个问题。

它不能：

- 运行 Shell、Python、JavaScript 或任意进程；
- 直接调用 COM、SelectionManager、FeatureManager 或保存命令；
- 读取/写入文件或加载 DLL；
- 自行联网检索产品规格；
- 发明工具 Schema 中不存在的圆角、倒角或其他操作；
- 绕过本地计划校验和执行后验。

## 4. 结构化决策协议

`PlanningDecision` 的固定字段：

| 字段 | 约束 |
| --- | --- |
| `action` | `create_plan` / `request_clarification` / `unsupported` |
| `task` | 创建计划时必填 |
| `reason` | 始终必填，最多 2000 字符 |
| `assumptions` | 最多 16 条；包含每个推断尺寸、近似和省略项 |
| `questions` | 仅澄清动作可用，最多 3 条 |
| `steps` | 仅创建计划可用，最多 64 步 |
| `confidence` | 0–1，仅用于诊断，不参与越权授权 |

未知字段由 Pydantic 拒绝。`request_clarification` 不能夹带执行步骤，`create_plan` 不能同时携带问题。

## 5. 主动假设策略

旧版 `RequirementPreflight` 会在模型调用前按正则硬拦截手机任务，必须输入
`161 × 78 × 8 mm` 和“简化主体”。v4.1 在已配置模型且全流程 Agent 开启时不再经过这层硬拦截。

新策略：

- “画一个手机模型”可直接生成近似包络计划；
- 计划预览显示例如“采用 160 × 78 × 9 mm 近似包络，不是官方精确规格”；
- 当前工具没有圆角/按键/接口时明确显示省略项；
- 用户可以执行、修改需求或重新生成，不会被同一个尺寸问题循环拦截；
- “创建一个与现有轴精密配合的孔”仍应询问轴径和间隙，因为猜测会改变装配结果。

若计划含数字几何参数，而需求中没有明确尺寸表达且 `assumptions` 为空，本地策略直接拒绝并要求
Agent 纠正。这防止“减少提问”退化成静默猜测。

## 6. 本地校验反馈闭环

需求规划 Agent 的输出验证器在最终返回前调用 `PlanningAgentPolicy`：

1. 把每个步骤转换为有限 JSON `ToolCall`；
2. 调用 `ToolRegistry.validate_plan`；
3. 检查工具名、未知字段、必填字段、数值范围、枚举和完整顺序；
4. 检查推断尺寸是否公开；
5. 失败时通过 PydanticAI `ModelRetry` 把精确拒绝原因返回同一会话；
6. Agent 在剩余请求预算内只修正被拒绝的字段或顺序。

最终返回后本地策略再次授权，`PlannerService` 还会执行一次注册表校验。模型不能通过一次格式正确的
输出绕过本地边界。

## 7. 兼容与关闭

- 全流程 Agent 默认开启，旧设置文件缺少该字段时也默认开启；
- 默认模型请求上限从 4 调整为 6，设置范围仍为 2–8；
- 关闭后，初始规划退回旧的 Chat Completions JSON 计划器和 `RequirementPreflight`；
- 未配置模型时继续使用本地确定性规划器，缺少关键尺寸时仍会澄清；
- ISO 4762 内六角螺栓继续优先使用本地标准表，不消耗模型请求。

## 8. 安全与隐私

- 本地路径和 UNC 路径替换为 `<local-path>`；
- `api_key`、Authorization、password、secret、token 字段及文本形式的赋值会脱敏；
- 上下文序列化后超过 200 KB 时拒绝发送；
- HTTP 客户端禁止自动跟随重定向；
- API 根地址保持用户配置，不再对根地址强制插入 `/v1`；
- 官方 DeepSeek Chat Completions 的结构化 Agent 请求显式关闭默认 thinking，避免强制输出工具的
  `tool_choice` 被端点拒绝；其他模型服务不受影响；
- API Key 只进入 Provider 认证头，不进入 Agent 工具结果；
- 实时上下文省略文档标题、文件路径、组件路径和持久引用。

## 9. 仍然存在的能力边界

Agent 能更聪明地组合现有工具，但不能弥补不存在的 CAD 原语。当前基础白名单仍没有圆角、倒角、
放样、扫描和曲面工具，因此手机任务只能建立无圆角近似包络或用现有圆/矩形/拉伸组合有限细节。

后续工具扩展必须逐个完成：Schema、参数校验、COM 原子执行、失败残留回滚、实际定义读回、几何后验、
纯测试和真实 SolidWorks 隔离冒烟。不能仅因为 Agent 会调用工具就把未经验证的 API 暴露给它。
