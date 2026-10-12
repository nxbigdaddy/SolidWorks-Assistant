# 设计文档总览

## V5 当前入口

- [5.1.2 调查空转与真实模型/CAD 验证](V512_PROGRESS_VALIDATION.md)
- [5.1.1 恢复修正、操作边界与验证（历史基线）](V511_RECOVERY.md)
- [5.1.0 持续 Agent 工作台（架构基线）](V51_AGENT_WORKBENCH.md)
- [5.1.0 验证报告（历史基线）](V51_VALIDATION_2026-09-06.md)
- [5.0 持续任务架构（历史基线）](V5_ARCHITECTURE.md)
- [V5 源码与实机验证报告（2026-09-06）](V5_VALIDATION_2026-09-06.md)
- [开发交接](../CODEX-HANDOFF.md)

## 继承的设计与历史报告

下面文档保留 V3/V4 设计背景；历史版本号、路径和验收结果不能直接套用于 V5。
V5 的数据隔离、任务流程与本轮验收，以以上专用文档及当前代码为准。

1. [产品需求](PRODUCT_REQUIREMENTS.md)
2. [系统架构](ARCHITECTURE.md)
3. [交互与状态流](WORKFLOWS.md)
4. [AI 计划协议](AI_PLANNING_PROTOCOL.md)
5. [SolidWorks 集成](SOLIDWORKS_INTEGRATION.md)
6. [纯 Python SolidWorks 稳定自动化层](STABLE_SOLIDWORKS_AUTOMATION.md)
7. [C# Add-in 事件桥与版本能力矩阵](EVENT_BRIDGE.md)
8. [本地模型库](LOCAL_MODEL_LIBRARY.md)
9. [数据库设计](DATABASE.md)
10. [界面设计](UI_UX.md)
11. [安全与隐私](SECURITY.md)
12. [测试策略](TEST_STRATEGY.md)
13. [构建、发布与运维](BUILD_RELEASE_OPERATIONS.md)
14. [旧版迁移](MIGRATION.md)
15. [实现状态与限制](IMPLEMENTATION_STATUS.md)
16. [内部接口与事件](INTERNAL_API.md)
17. [错误目录与恢复](ERROR_RECOVERY.md)
18. [2026-08-18 全量代码审计与修复报告](CODE_AUDIT_2026-08-18.md)
19. [2026-09-01 基础建模稳定性收口报告](SOLIDWORKS_BASE_MODELING_STABILITY_2026-09-01.md)
20. [开发与验收操作指南](OPERATIONS_GUIDE.md)
21. [当前验证报告](VALIDATION_REPORT.md)
22. [MCP 只读集成](MCP_INTEGRATION.md)
23. [12 项能力迭代规格与验收结果](ROADMAP_12_CAPABILITIES.md)
24. [系统功能报告（HTML）](SYSTEM_FUNCTION_REPORT.html)
25. [产品介绍与操作指南（16:9 横版 HTML）](PRODUCT_PRESENTATION_REPORT.html)
26. [2026-09-01 计划生成可靠性报告](PLANNING_RELIABILITY_2026-09-01.md)
27. [受约束 SolidWorks 监督 Agent 架构](AGENT_SUPERVISOR_ARCHITECTURE.md)
28. [Agent 版迁移与回退指南](AGENT_VERSION_MIGRATION.md)
29. [2026-09-02 Agent 版验证报告](AGENT_VERSION_VALIDATION_2026-09-02.md)
30. [v4.1 全流程建模 Agent 架构](FULL_AGENT_ARCHITECTURE.md)
31. [v4.1 全流程 Agent 验证报告](FULL_AGENT_VALIDATION_2026-09-02.md)
32. [2026-09-03 DeepSeek V4 Agent 兼容性修复](DEEPSEEK_AGENT_COMPATIBILITY_2026-09-03.md)
33. [v4.2 Pi 持久恢复 Agent 集成](PI_AGENT_INTEGRATION.md)
34. [2026-09-03 v4.2 Pi Agent 验证报告](PI_AGENT_VALIDATION_2026-09-03.md)

所有文档使用 UTF-8。代码行为与文档冲突时，以安全约束更严格的一方为准，并在
同一次变更中修正文档。
