# 实现状态与限制

本文是设计目标与当前源码之间的事实清单。只有“已实现且已测试”的项目才能作为源码验收结论；
涉及真实 SolidWorks、EXE 或特定版本 API 的项目还需要对应环境验收。

## 1. 已实现并通过纯自动化测试

| 能力 | 实现位置 | 验证重点 |
| --- | --- | --- |
| 结构化在线/离线规划 | `planning`、`tools.py` | JSON、工具白名单、参数范围、阶段反馈 |
| 受约束失败监督 Agent | `agent`、`runtime`、`ui.settings_dialog` | 只读检查、结构化决策、本地二次授权、请求限额、人工审查回退 |
| Pi 持久多轮恢复 Agent | `agent.pi_client`、`agent.pi_supervisor`、`runtime` | 同任务会话复用、校验拒绝反馈、2–12 轮预算、工具全禁用、自动回退 |
| 全流程需求规划 Agent | `agent.requirements`、`agent.planning_policy`、`planning`、`ui` | 主动假设、必要澄清、工具检查、同会话校验纠错、假设可见化 |
| ISO 4762 标准件规划 | `planning`、`tools.py` | M3-M12 参数表、公称长度语义、端面建模、内六角切除 |
| 需求澄清与计划预览 | `planning`、`ui.plan_preview` | 缺失关键尺寸不调用模型、二维轮廓与特征摘要 |
| 单步执行状态机 | `execution` | 前后快照、后验失败、保留成功前缀、有限重试 |
| 纯 Python 稳定拓扑层 | `solidworks.topology`、`solidworks.serial_gate` | 重建后面重解析、唯一候选、项目自有跨进程互斥和失效拒绝 |
| 分级自动失败恢复 | `execution.recovery`、`runtime`、`ui` | 暂时性错误单次自动重试、脱敏 AI 后缀修复、Pi 任务级预算、脏模型停止；保留改参数、人工验证和跳过 |
| 停止全部 | `solidworks.client`、`runtime` | 取消批任务、终止 COM 助手进程、阻止后续步骤 |
| C# Add-in 事件消费端 | `src-dotnet`、`solidworks.event_bridge` | JSONL 协议、去重、轮转、能力反射、IDispatch 回归 |
| 本地模型库 | `library` | 快速扫描、分页、哈希、缺失标记、增量深读、停止 |
| Document Manager可选读取 | `library.document_manager` | 无界面属性读取、许可证缺失时安全关闭、部分状态标识 |
| Manifest schema 2 + 库 schema 3 | `solidworks.gateway`、`library.repository` | 模型参数快照；批任务、语义索引、操作学习和能力运行记录 |
| 零件复制与参数派生 | `library.derivation` | 不覆盖、临时副本、源哈希、重开验证、失败清理 |
| 装配体 Pack and Go | `library.pack_and_go` | 官方 API 边界、不覆盖、临时目录、源哈希、状态与文件验证 |
| 安全格式导出 | `library.exporting` | 格式白名单、临时文件、不覆盖、源与输出哈希 |
| 模型库分析 | `library.analysis` | 参数差异、引用图、质量问题、相似度透明评分、停止 |
| MCP只读基座 | `integration.mcp_server` | 协议中立门面测试；SDK为可选依赖，不开放写操作 |
| 旧版数据迁移 | `migration`、`runtime` | 只读旧库、去重导入、历史映射、凭据重新输入 |
| 设置、审计与历史 | `infrastructure` | DPAPI、脱敏、原子设置写入、状态回写 |
| SolidWorks 内嵌 Task Pane 与实时上下文 | `src-dotnet`、`solidworks.event_bridge` | 文档级事件、选择快照、过期控制、桌面端启动入口 |
| 模型健康与制造诊断 | `advanced.health`、`advanced.manufacturing` | 规则、严重度、证据、建议、PMI 与 DFM 能力门控 |
| 产品族与装配体助手 | `advanced.product_family`、`advanced.assembly` | 配置矩阵、稳定尺寸身份、组件/干涉/缺件检查、持久引用配合 |
| 工程图自动化 | `advanced.drawing`、`solidworks.gateway` | 三视图、等轴测、剖视图、BOM、气泡、尺寸导入和 PDF/DWG |
| 持久化批处理 | `automation.jobs` | 租约、心跳、暂停/恢复/取消、崩溃恢复、幂等项、1000 项压测 |
| 语义搜索与人工操作学习 | `advanced.semantic_search`、`advanced.learning` | 本地加权检索、可解释命中、候选配方审批和引导回放 |
| PDM/Document Manager 与能力实验室 | `advanced.pdm`、`advanced.capability_lab` | 只读适配、许可证门控、版本证据、发布门禁 |
| PySide6 无系统标题栏 UI | `ui` | 建模、模型库、诊断搜索、自动化、上下文、恢复与统一停止 |

当前纯测试数量以实际 `pytest` 输出为准，不在本文写死。

## 2. 已实现能力的实机覆盖与版本边界

- SolidWorks 2025 SP5.0 已完成 14/14 基础建模稳定性矩阵：模板优先的新建零件、三种标准基准面、
  圆/矩形/直线/中心线、盲拉伸/中面/反向、拉伸凸台/切除、旋转凸台/切除和反向旋转。
- 正多边形已改为官方 `CreatePolygon` 原子路径，且不再把非空 COM 返回值直接当作成功：必须在
  重绘/重建后观察到草图段增加；确认无提交时限次重试一次，再进入带完整性读回和命令级残留清理的
  逐边回退。右视面采用
  `local(x,y)->world(Y,Z)` 坐标约定。上一拉伸特征端面草图和小尺寸几何继续使用经验证的精确创建；M4×30 内六角圆柱头螺钉
  已通过本机 12 步不保存冒烟、两次端面重解析及解析体积校验，其余规格仍需版本矩阵回归。
- 拉伸端面已由跨步骤缓存 `Face2` 改为持久引用与几何签名重解析；纯测试已证明重建后使用新的面对象并拒绝
  多个同等候选；SolidWorks 2025 实机已通过多级端面建模。
- 旋转中心线已由跨步骤缓存 `ISketchSegment` 改为 `ISketch.Name`、草图/线段持久引用、官方线段
  ID/type、端点和构造线属性；实机关闭草图并重建后均通过持久引用取得当前对象，并严格核对 Mark=0/4。
- 草图坐标框架已绑定文档、配置和活动 `ISketch` 身份；实机切换到第二个未保存零件时在写 API 前停止。
  拉伸/旋转已接入官方 `ISketch.CheckFeatureUse`，开放轮廓实机保持活动草图且未创建实体。
- 拉伸/旋转特征通过显式空 `IDispatch` 读取定义，处理 Instant3D `ICE` 类型，并核对
  `IFeature.GetErrorCode2`；故意制造定义不一致后实机 `EditUndo2(1)` 恢复原特征数、实体数和体积。
- COM 文档深度读取、尺寸写入、重建、保存与重新打开验证。
- SolidWorks 2025 SP5.0 已完成两个产品族配置的写入、重建、保存与重开验证。
- SolidWorks 2025 SP5.0 已完成装配体组件/缺失引用/干涉读取，以及基于持久引用的平行配合创建、重建、撤销和源哈希不变验证。
- SolidWorks 2025 SP5.0 已完成三视图、等轴测、剖视图、BOM、自动气泡以及 PDF/DWG 导出和重开检查。
- PMI 读取 API 在 SolidWorks 2025 SP5.0 上可调用；当前夹具没有 PMI，因此记录数为 0，不代表带标注夹具已验收。
- 停止时终止助手进程后，SolidWorks 对已经受理的当前原子操作的实际行为。
- C# Add-in 已在 SolidWorks 2025 SP5.0 上验证自动加载、Task Pane、文档上下文和 25/25 能力探测；
  2024/2026 等未安装主版本仍需能力实验室实机回归。

基础建模的当前版本证据见 `SOLIDWORKS_BASE_MODELING_STABILITY_2026-09-01.md`。其他 SolidWorks 主版本、
企业接口和高级夹具仍必须按 `TEST_STRATEGY.md` 的对应矩阵执行，不能从 2025 SP5.0 结果外推。

## 3. 安全关闭或尚未开放

- 工程图普通复制：仍明确拒绝；装配体必须使用 Pack and Go。
- 复杂零件重绘：纯 Python 白名单覆盖圆、矩形、正多边形、直线、中心线、上一特征平面端面、基础拉伸
  和旋转；仍不猜测任意圆角、孔向导、曲面、放样或扫描。
- 第三方建模 DLL/Python.NET：明确不进入旧项目；稳定性能力必须由可审计的本项目 Python 源码实现。
- ISO 4762 当前生成公称直径实体、圆柱头和内六角孔；真实螺旋牙型或装饰螺纹尚未加入工具协议。
- SolidWorks Document Manager 完整版本矩阵：已实现可选元数据适配器，但仍需使用合法许可证密钥在目标
  SolidWorks 版本上回归配置、属性和外部引用的 COM 签名。
- PDM Professional 只读适配已实现，但当前机器没有安装 PDM COM，能力实验室会明确报告不可用。
- 自动日志清理、安装包签名和在线更新：不属于当前源码门禁。
- 安装程序已生成并通过隔离安装/卸载、内部哈希、EXE 冷启动和内容审计；但未配置商业 Authenticode 证书，不能标记为已签名发布。

## 4. 停止语义

“停止全部”立即使计划请求失效、设置批任务取消事件、终止 COM 助手进程，并阻止任何后续步骤。
SolidWorks API 多数是同步原子调用，没有通用的跨进程撤销接口；若 SolidWorks 已经受理当前调用，
该动作可能在助手终止前完成。界面因此要求检查当前模型状态，继续时会建立新工作进程并重新快照。
