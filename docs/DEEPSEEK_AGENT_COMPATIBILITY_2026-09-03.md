# DeepSeek V4 Agent 兼容性修复

验证日期：2026-09-03（Asia/Shanghai）<br>
修复版本：4.1.2

## 1. 故障

需求规划在任何 SolidWorks 写入之前返回：

```text
status_code: 400
model_name: deepseek-v4-pro
Thinking mode does not support this tool_choice
```

当时的配置是官方 `https://api.deepseek.com` 和 `deepseek-v4-pro`。因此问题不在草图、面选择或 COM，
而在模型请求协议。

## 2. 原因

DeepSeek V4 默认开启 thinking。需求规划 Agent 使用 PydanticAI 的严格 `PlanningDecision` 输出；
PydanticAI 把这个最终结果表示为输出工具，并在不能接受普通文本时使用 `tool_choice=required` 或指定
输出工具。实际 DeepSeek Chat Completions 端点拒绝了 thinking 与该强制选择的组合。

DeepSeek 官方文档说明 thinking 默认开启，并支持通过 Chat Completions 的
`extra_body={"thinking":{"type":"disabled"}}` 关闭：

- <https://api-docs.deepseek.com/guides/thinking_mode/>
- <https://api-docs.deepseek.com/api/create-chat-completion/>

## 3. 修复

新增统一 Provider 兼容层 `agent/provider_compat.py`：

- 仅当主机是 `api.deepseek.com` 或其官方子域时发送
  `{"thinking":{"type":"disabled"}}`；
- 需求规划 Agent 和失败恢复 Agent 使用同一设置；
- 继续使用三个只读检查工具、严格结构化输出、本地 `ToolRegistry`、输出验证器和有限重试；
- DashScope 原有 `enable_thinking=false` 行为保持不变；
- 任意其他 OpenAI-compatible 地址不添加 DeepSeek 参数。

这不是按错误字符串无限重试，也没有把模型输出直接交给 SolidWorks。

## 4. 验证

- DeepSeek/DashScope/通用端点三项 Provider 单元测试通过；
- 需求规划与失败监督 Agent 相关 19 项定向测试通过；
- 使用用户已配置的官方 DeepSeek 端点发送合成请求“创建一个 10 × 10 × 10 mm 的测试长方体，仅生成
  计划，不保存”；约 14.9 秒返回 6 步计划：连接、新建零件、基准面、开始草图、矩形、拉伸；
- 返回计划通过本地 Schema 和顺序校验；没有执行 SolidWorks；
- 探针未发送日志、CAD 内容、文件路径或故障上下文。
- 全量门禁为 234 项 pytest、Ruff、mypy（65 个源码文件）和 compileall；Event Bridge 8/8 通过；
- 4.1.2 正式目录与隔离安装目录均冷启动 8 秒并正常关闭。打包同时排除了会遮蔽 Windows ICU 的
  外来 `icuuc.dll`，并携带 `genai_prices` / `pydantic_ai_slim` 元数据，避免 QtCore 和 Agent 在
  用户看到主窗口前失败。

## 5. 边界

该兼容策略针对官方 DeepSeek Chat Completions。通过第三方代理访问同名模型时，代理可能采用不同
协议，程序不会仅凭模型名向未知主机注入 DeepSeek 专用字段。若未来迁移到 DeepSeek Responses API，
应建立独立的工具调用、结构化输出、reasoning 回传和请求预算回归后再切换。
