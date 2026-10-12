# AI 计划协议

v4.1 默认使用全流程需求规划 Agent。下述旧式 `{task, steps}` JSON 仍是关闭 Agent 时的兼容协议；
默认 Agent 的外层输出为严格 `PlanningDecision`：

```json
{
  "action": "create_plan",
  "task": "建立手机近似包络主体",
  "reason": "当前工具可安全完成简化实体",
  "assumptions": ["采用近似包络尺寸，不是官方精确规格", "省略圆角和接口"],
  "questions": [],
  "steps": [],
  "confidence": 0.72
}
```

完整决策、只读检查工具和本地反馈闭环见 `FULL_AGENT_ARCHITECTURE.md`。

## 1. 输出格式

```json
{
  "task": "创建圆柱",
  "steps": [
    {"tool": "session.ensure", "arguments": {}, "description": "连接 SolidWorks"},
    {"tool": "document.new_part", "arguments": {}, "description": "新建零件"},
    {"tool": "selection.plane", "arguments": {"plane": "front"}},
    {"tool": "sketch.begin", "arguments": {"plane": "front"}},
    {"tool": "sketch.circle", "arguments": {"x_mm": 0, "y_mm": 0, "radius_mm": 20}},
    {"tool": "feature.extrude", "arguments": {"depth_mm": 30, "operation": "boss"}}
  ]
}
```

兼容 JSON 顶层只能包含任务和步骤；Agent 协议还允许受约束的假设字段。每个工具名必须存在于注册表；未知字段、缺失必填字段、非有限数字、
越界尺寸和不允许的枚举值均拒绝。尺寸在协议中使用毫米和度，只有 COM 边界转换系统单位。
参数校验后还要运行本地顺序状态机：可写零件必须先存在，草图几何必须位于活动草图内，拉伸前草图
必须含几何，切除前必须已有实体，同一计划不能中途替换当前文档。
直线、中心线和旋转遵守同一状态机：旋转前必须有轮廓和唯一中心线，旋转切除前必须已有实体。
直线轮廓还必须形成非零面积的闭合环；中心线不参与闭合判定。圆、中心矩形和正多边形作为原生闭合
轮廓处理。已有实体上继续建模时使用 `sketch.begin_face` 定位上一特征的最外侧平面端面，不能用原点
基准面代替实际端面。

每个工具的 `arguments` 现在使用标准 JSON Schema 子集描述：`type`、`properties`、`required`、
`additionalProperties=false`、`enum`、`minimum`、`maximum` 和 `default`。规划模型看到的协议与本地
验证器来自同一个 `ValidationRule`，不会再出现“本地只接受 `front/top/right`，模型描述却没有给出
枚举”的双轨规则。模型必须原样返回内部枚举，不能翻译。

在严格校验前，本地只对有限的无歧义别名进行规范化。例如 `Front Plane`、`前视基准面` 转换为
`front`，`拉伸切除` 转换为 `cut`，`中面` 转换为 `mid_plane`。未知值仍被拒绝，错误同时携带步骤
序号、工具、字段、收到值和期望 Schema；别名兼容不会绕过工具白名单或顺序状态机。

未配置模型或关闭全流程 Agent 时，模型调用前仍运行确定性需求预检。圆缺少半径/直径、矩形缺少
长宽、拉伸/切除缺少深度或孔缺少直径时，返回 `planning_clarification` 与问题列表。

全流程 Agent 开启时，手机等普通外形不再被尺寸正则提前拦截。Agent 可以采用合理近似包络并把每个
推断尺寸、省略的圆角/摄像头/按键/接口写入 `assumptions`；本地策略禁止静默猜测和虚构工具。只有
配合、接口、制造、安全或明确要求的精确规格才允许转为 `planning_clarification`。

ISO 4762 内六角圆柱头螺钉属于确定性标准件：当需求包含 `M规格×长度` 时，本地标准件编译器优先于
模型服务，按公称直径、头径、头高、内六角对边和孔深生成轴、头部及内六角切除步骤。当前参数表覆盖
M3、M4、M5、M6、M8、M10 和 M12；公称长度按头部以下长度解释。这样避免模型猜测标准尺寸、把
内六角误画成圆孔或把公称长度错误拆成总长。

## 2. 规划阶段

UI 显示请求生命周期和可验证的阶段摘要，不显示隐藏思维链。结构化 JSON 可以记录在本地审计
日志中；Authorization、API Key、服务端隐藏推理和完整系统提示词不能记录。

## 3. 修复协议

修复请求包含已完成步骤、失败工具、参数、错误码、执行前后快照和剩余步骤。模型只返回从失败
位置开始的新后缀。验证器把已完成前缀与替代后缀临时拼接后执行同一顺序校验，再只提交后缀；模型
不能重复建文档，也无权把失败步骤标为成功。

兼容 JSON 规划器首次生成的计划如果仅在本地工具/顺序校验阶段失败，自动执行最多一次定向修复：保留失败
步骤之前已经验证的前缀，把失败步骤、剩余后缀、结构化错误和完整工具 Schema 交给同一模型，只要求
返回替代后缀。修复结果与前缀重新运行完整状态机；第二次仍失败就停止，不进行无界重试。

规划失败历史在原有 SQLite 记录中保存失败计划和结构化诊断，审计日志记录失败记录 ID 与诊断摘要。
API Key、Authorization 和服务端隐藏推理仍由日志脱敏层排除。

## 4. 供应商兼容

基础协议使用 `/chat/completions`。供应商扩展参数只由 `ModelClient` 根据主机名添加。重定向默认
禁止，防止 Authorization 头跨域泄露；响应上限 2 MB；超时、401、429 和无效 JSON 转换为
用户可理解的错误。全流程 Agent 通过 PydanticAI OpenAI-compatible Provider 调用同一协议，保持
用户配置的 API 根路径，不对根地址隐式插入 `/v1`，并同样禁止重定向。

DeepSeek V4 的 Chat Completions 默认开启 thinking，而 PydanticAI 的严格结构化最终结果会作为输出
工具并使用强制 `tool_choice`。官方 DeepSeek 端点若拒绝这个组合，规划与恢复 Agent 会显式发送
`{"thinking":{"type":"disabled"}}`；只关闭该请求的 thinking，不关闭工具调用、本地 Schema 校验、
输出验证器或失败重试。其他 OpenAI-compatible 主机不会继承这项 DeepSeek 专用参数。
