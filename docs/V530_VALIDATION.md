# 5.3.0 迭代验证记录

日期：2026-09-07。开发目录：`SolidWorks-Assistant`。
基线：`v5.2.1-baseline` / `b6493ac`。分支：`codex/v5.3-reliability-core`。

## 当前结论

本轮已实现受控 Pi 阶段执行、持久检查点与显式恢复、生产 RPC 进度与文档绑定。
源码最终全量门禁通过：**1491 passed / 389.90 秒**，Ruff、mypy（85 个源文件）、compileall
和 `git diff --check` 通过。命令为 `.\scripts\verify.ps1`，退出码 0。
首次真实 SolidWorks 测试未通过，因此本文件不把此版本标为已完成真实恢复验收或可发布安装包。
当前三个测试文档仍保留，等待用户处理后重新执行空现场实测。

## 验证分层

| 验证 | 证据与范围 |
| --- | --- |
| 根执行边界 | 121 项 worker/写前检查/文档绑定针对性测试通过，含禁止重定向、进入执行后的未知状态 |
| Pi 交互链路 | 真实本机 Pi + localhost 脚本化接口 + TaskAgentBackend + runner + 模拟 CAD 副本，一次 decide 内失败恢复、替代候选、宿主最终验收；无外部模型请求 |
| 持久检查点 | 106 项 fake 原生存储/保存/恢复测试通过；后续加入路径预检查后 113 项目标回归通过 |
| 停止竞态 | 包括最后一次 deadline 读取时触发 interrupt 的 5 类 RPC 故障注入；相关 168 项回归通过 |
| GUI/控制器 | 检查点按钮、离线记录恢复、busy+ready 启动进度、停止交错、第二个独立任务 fresh scope 均有自动回归 |
| Windows 窗口视觉 | computer-use 截图接口返回 `SetIsBorderRequired ... 0x80004002`；文字读取未提供可用树，停止截图尝试。未完成新 UI 视觉验收 |
| 真实 SolidWorks | 生产 runner/client/spawn/worker 路径已运行，但第一次在检查点保存阶段未通过，详见下文 |

上述分组存在测试重叠，不能相加作为最终全量数量。最终 1491 项门禁包含本轮所有已落盘修正，
包括路径、结束回执、取消 finalize、控制器代次和离线恢复修正，统一由 `scripts/verify.ps1` 执行。

## 首轮全量回归发现并修正的整合问题

首轮为 **6 failed, 1445 passed / 374.83 秒**，均集中在 workbench runtime 关联测试。

1. 为防取消代次被嵌套 `operation()` 洗掉，内层开始保留外层代次；但控制器先绑定外层、再停止
   上一个 runner，会使新任务继续沿用已取消代次。修正为准备/重建恢复两个入口在内部停止与
   generation 检查后明确绑定新 scope，其余后台任务默认绑定策略不变。
2. 离线检查点恢复增加真正执行前的模型配置校验后，旧 StubRunner 测试缺少 synthetic 配置。
   仅给 workbench 专用 fixture 添加 localhost 假配置，不放宽真实执行校验。
3. 新增替换 runner 期间和刚绑定新 scope 时的用户 Stop 交错测试；两类入口均不得继续写入。

修正后的 41 项完整关联回归通过；之后最终 1491 项全量复跑通过，没有用目标测试取代完整门禁。

## 真实生产 RPC 测试：第一次

运行命令：

```powershell
.\.venv\Scripts\python.exe scripts/live_production_reliability_smoke.py --execute --start-solidworks --verify-checkpoint-restore
```

证据：
`artifacts/production-reliability-smoke/1788766905015959500-b9f9a4c2/result.json`，耗时 **297.97 秒**，
退出码 **1**，`passed=false`，任务状态 `review`。

实际发生的顺序：

1. 启动前通过无活动文档、全部打开文档数为零的检查。
2. 创建新的空白测试零件；所有 CAD 写入通过独立生产工作进程，不使用 InlineWorker。
3. 故意构造宽度不合格候选，体积为 990 mm³，局部检查失败。
4. 自动切回仍保存的原模型，并独立读取验证；记录 `trial_restored`。
5. 修正候选达到 693 mm³，通过体积、实体数、草图关闭和特征健康检查。
6. `finish(accept=True)` 在持久检查点保存阶段未确认成功；宿主暂停，未继续切孔或恢复检查点。

现场保留了三个新建测试文档，程序没有关闭、删除或自动接管它们。没有编辑用户已有图纸，
也没有调用外部模型。本次固定本地后端不是实际模型自主规划质量的证明。

### 保存问题的证据与修正

失败检查点目录已创建，原生检查点文件及 manifest 未生成。旧代码构造的完整路径长度为 **268**。
SolidWorks 官方错误枚举对 `swFileSaveAsNameExceedsMaxPathLength`（2048）记录了 255 字符限制。
这是一个确定的路径设计缺陷，但第一次结束回执未保留原始错误码，因此不能仅凭旧日志断言
没有其他保存原因。[官方错误码说明](https://help.solidworks.com/2025/english/api/swconst/SOLIDWORKS.Interop.swconst~SOLIDWORKS.Interop.swconst.swFileSaveError_e.html)

修正：原生文件名不再重复完整长前缀与完整 UUID，任务 hash、完整检查点目录 ID 和任务绑定
manifest 保留；保存/打开前按 UTF-16 单元校验 255 上限。保存失败保留 success/errors/warnings，
宿主 finish_receipt 保留有界原因与验证差异，避免再次只有泛化消息。

这些修正通过离线测试，但尚需关闭本轮三个测试文档后重新运行空现场实测。不得为了重跑而
绕过空现场检查、强制关闭 SolidWorks 或接管旧候选。

## 尚未覆盖

- 完整生产实机 `restored → committed → committed → checkpoint restored` 及独立最终几何验收。
- 真实 SolidWorks 的用户 Stop/进程中断现场恢复；当前取消故障注入是离线验证。
- 外部真实模型 + Pi 原生阶段工具 + 生产 CAD 工作进程的一条完整新链路。
- 复杂手机、多配置、装配体、跨 SolidWorks 版本、任意几何及新 UI 视觉验收。
- EXE、安装器、冷启动、安装/卸载和部署包内容审计。本轮没有构建发布包。

C# 执行 Add-in 尚未实现；兼容固定计划尚未整体迁移到新阶段/检查点和写前协议。
源代码落盘与自动回归通过，不等同以上未覆盖项目已经完成。
