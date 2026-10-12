# 当前验证报告

验证日期：2026-09-03（Asia/Shanghai）；基础建模实机证据最后执行于 2026-09-01。

## 0. v4.2.0 Pi 持久恢复 Agent 增量结论

- 同一计划复用受限 Pi RPC 会话；JSON/Schema/本地策略拒绝原因会反馈给同一会话继续修正，默认每个
  任务 6 轮，可设置为 2–12。Pi 工具、扩展发现、技能、项目上下文和本地信任全部禁用。
- 自动后端优先 Pi；同一计划首次 Pi 启动或协议失败后明确记录并回退内置 PydanticAI，不会在后续失败
  中重复卡 Pi。强制 Pi 模式不静默回退。
- 全量 `pytest -q` 245 项通过；Ruff、mypy（67 个源码文件）、compileall 通过；真实 Pi 0.84.4 CLI
  对本机 OpenAI 兼容 SSE 假端点完成端到端恢复，模型请求不含工具字段，未连接第三方模型。
- PyInstaller、Event Bridge 0 警告/0 错误与 8/8 测试、Setup 和 portable 构建通过。正式目录和隔离
  安装目录冷启动 8 秒时窗口响应正常、标题正确并退出码 0；隔离卸载成功。
- 发行目录 392 个文件、106 个 DLL；与本机 01yx 的 6 个 DLL 做 SHA-256 比对重合为 0。名称中无
  `01yx/OneYx/Pi GUI`，无数据库、日志、CAD、PDB 或凭据载荷。

完整证据和产物哈希见 `PI_AGENT_VALIDATION_2026-09-03.md`。

## 0.1 v4.1.2 全流程 Agent 增量结论

- 独立目录保持为 `<workspace>/solidworks_tools_agent`，旧项目未覆盖；需求规划 Agent 与失败监督 Agent
  均通过 PydanticAI 2.37 运行，写操作仍只由本地白名单执行器完成。
- `pytest -q`：234 项通过；Ruff、mypy（65 个源码文件）和 compileall 通过；C# Event Bridge
  Release 0 警告、0 错误，8/8 自包含测试通过。
- 最新手机任务回归证明：普通外形可生成 160 × 78 × 9 mm 的近似包络计划并显示两个假设，不会再次
  强制补充尺寸；精密配合任务仍会询问公称直径与间隙。
- 本地 OpenAI 兼容假端点完成 3 次请求的工具检查与结构化输出闭环；官方 DeepSeek 端点完成一次仅含
  合成 10 mm 长方体和工具 Schema 的计划探针，未发送日志、CAD 数据、路径或故障上下文。
- 实机发现 `CreatePolygon` 偶发返回 COM 对象但草图实体数不增加。修复后只接受经重绘/重建确认的
  实体增长，允许一次确认无副作用的原生重试，否则进入带完整性校验/回滚的逐边后备；SolidWorks
  2025 SP5.0 隔离 8 步场景随后连续 3/3 通过。
- PyInstaller one-folder、4.1.2 Setup 和 portable ZIP 构建成功；正式 `dist` 与隔离安装目录分别冷启动
  8 秒并正常关闭。构建会删除缺少 `_internal` 的 PyInstaller 中间 EXE，排除会遮蔽 Windows ICU 的
  外来 `icuuc.dll`，并显式携带 PydanticAI 元数据；启动器、构建和安装脚本均做对应门禁。
- 官方 DeepSeek `deepseek-v4-pro` 默认 thinking 与 PydanticAI 严格输出工具的 `tool_choice` 冲突已
  修复；Agent 请求对官方 DeepSeek Chat Completions 显式使用 non-thinking。真实端点合成 10 mm
  长方体请求已返回并通过本地 6 步计划校验，未执行 SolidWorks。
  本程序 106 个发行 DLL 与本机 01yx 的 6 个 DLL 做 SHA-256 比对，重合为 0；发行目录/ZIP 的
  `01yx/OneYx` 名称命中和运行时禁用引用命中均为 0。

完整哈希、产物和边界见 `FULL_AGENT_VALIDATION_2026-09-02.md`。

## 0.2 v4.0.0 Agent 版历史增量结论

- 在独立目录 `<workspace>/solidworks_tools_agent` 和无全局 site-packages 的 `.venv` 中验证；旧项目未覆盖。
- `pytest -q`：215 项通过；Ruff、mypy（62 个源码文件）和 compileall 通过。
- Agent 新增测试覆盖只读检查闭环、结构化停止/审查、模型变化拒绝、失败调用去重、非有限参数拒绝、
  路径/凭据脱敏、200 KB 上限和本地 OpenAI 兼容 HTTP 三轮工具调用。
- PyInstaller one-folder 构建成功，发布进程离屏冷启动 6 秒后仍正常运行并已清理。
- 新版本源码的 SolidWorks 2025 SP5.0 新建未保存零件 8 步实机冒烟通过；证据为
  `artifacts/live-agent-hex-key-corner-2026-09-01.json`。
- 本轮没有调用外部模型端点，没有向第三方发送日志、CAD 数据或故障上下文；真实云模型恢复仍待
  获得明确数据传输授权后单独验收。

v4 的完整边界和哈希证据见 `AGENT_VERSION_VALIDATION_2026-09-02.md`；以下内容保留 v3 稳定层的
既有验证历史。

## 1. 源码门禁

- `pytest -q`：196 项通过；此前覆盖率审计为 64%（覆盖率数值尚未按本次新增代码重算）。
- `ruff check launcher.py src scripts tests`：通过。
- `mypy src`：56 个源码文件零问题；`scripts/live_solidworks_smoke.py` 单独检查零问题。
- `compileall launcher.py src scripts tests`：通过。
- C# Event Bridge Release：MSBuild 0 警告、0 错误；8/8 自包含测试通过。
- 持久化批处理：1000 个独立提交任务项全部完成，核心运行约 1.26 秒；同时覆盖租约、暂停/恢复/取消、超时恢复和“停止全部”。

自动化覆盖 12 项能力的领域模型、Fake 适配器、schema 2→3 事务迁移、语义检索解释、配方审批/引导回放、能力门控、工程图 API 版本回退、COM 输出参数和 UI 状态。

本轮审计新增了非 XY 草图坐标、特征失败撤销、参数化后验验证、排队任务暂停、执行前上下文门禁、恢复请求竞态、能力快照进程/版本/时间匹配、关键事件拥塞保护、日志保留清理和自定义安装目录卸载边界的回归覆盖。

2026-09-01 增量验证：

- 修复模型可见工具协议缺少类型、枚举和范围的问题；全部参数 Schema 由本地验证规则直接生成，
  `plane` 等字段不再依赖提示词猜测。
- 常见中英文基准面、特征操作和终止条件别名会规范化为内部枚举；未知值继续严格拒绝，并输出步骤、
  字段、收到值和期望 Schema。
- 模型计划首次校验失败时只自动修复一次失败后缀；复杂手机请求缺少尺寸或简化范围时在联网前转为
  明确澄清。失败计划和结构化诊断已持久化到历史记录。
- 新增 18 项规划/诊断/UI 回归后，全量 196 项 pytest、Ruff、mypy（56 个源码文件）和 compileall
  通过；其中键盘事件测试覆盖补充条件输入与重新提交，空输入不会重复请求。

- 新增测试证明拉伸重建后按几何签名取得新的面对象、持久引用几何不符时回退重解析、歧义端面被拒绝、
  失败调用改变拓扑后禁止原样重试。
- 新增测试证明草图关闭/重建后按官方线段 ID/type 或端点签名取得新的中心线对象，重复中心线造成歧义时
  拒绝猜测。
- 新增测试证明活动文档/草图漂移会在几何写入前停止，未绑定草图不能借助残留选择拉伸，开放轮廓不会
  进入特征 API，失败后快照在串行锁释放前完成。
- 建模运行时保持纯 Python/pywin32，使用项目自有 Windows 命名互斥锁；源码和测试不依赖第三方建模 DLL
  或 Python.NET。
- 实机暴露并修复三项 pywin32 差异：`ISketch` 没有 `GetFeature`、`AccessSelections` 需要显式空
  `IDispatch`、Instant3D 的 `GetTypeName2()` 返回 `ICE`。相应回归已加入自动测试。
- `ruff 0.9.10`、mypy、compileall 和全量 pytest 均通过；本轮已连接 SolidWorks，并只操作脚本创建的
  未保存零件。
- 运行源码/脚本/测试/依赖清单中的 01yx、Python.NET、CLR/DLL 显式加载命中为 0。运行源码目录 DLL
  数量为 0；项目现有 143 个构建/发布 DLL 与本机 01yx 目录当前发现的 6 个 DLL 做 SHA-256 比对，
  相同哈希为 0。该外部样本数量是 2026-09-01 本次复核快照，不沿用旧快照的 11 个计数。

## 2. SolidWorks 2025 SP5.0 实机回归

环境：SolidWorks Premium 2025 SP5.0，修订号 `33.5.0`，Interop `33.5.0.53`。

- 基础建模稳定性矩阵：14/14 场景通过，0 个保存路径，结束后 0 个残留 SolidWorks 进程。覆盖前/上/右
  基准面、盲拉伸、中面、反向拉伸、旋转凸台/切除、反向旋转、开放轮廓拒绝、定义不一致撤销和跨文档
  漂移保护。
- 特征证据：10 个 Boss Extrude、1 个 Cut Extrude、2 个 Boss Revolve、1 个 Cut Revolve；14/14
  `CheckFeatureUse` 状态为 0，14/14 `GetErrorCode2` 为 0 且无警告；所有旋转选择均为总数 2、
  Mark=0 草图 1、Mark=4 中心线 1。
- 多级建模：M4×30 ISO 4762 夹具的 12 步建模、两次端面重解析、后验体积和不保存关闭通过。
- 产品族：从源零件生成 `规格-A` 8 mm/A 和 `规格-B` 12 mm/B，重开后尺寸、配置属性和源哈希均验证。
- 装配体：读取 2 个组件（一个固定、一个自由），无缺失引用，干涉检查 API 已执行。
- 配合：持久引用恢复两个平面，`CreateMateData/CreateMate` 创建“平行1”，重建成功；随后撤销、不保存关闭，源装配体 SHA-256 仍为 `8AE7773490B49F5CD3BD1F948C35BE2F03D20E31ABE36CC45E31E91E04ED8849`。
- 工程图：主视/俯视/右视/等轴测、剖视图、BOM、自动气泡和模型尺寸导入已在重开 manifest 中检查。
- 导出：剖视工程图成功导出 PDF 和 DWG，分别为 46,534 和 39,568 字节，且都有非空 SHA-256。
- PMI/DimXpert：API 读取路径成功；夹具没有 PMI，返回 0 条记录，未把“空数据”误报为带 PMI 夹具验收。
- PDM Professional：当前机器没有 PDM COM，只读适配器和能力实验室正确返回“不可用”。

基础建模证据位于 `artifacts/live-solidworks-smoke-2026-09-01.json`，详细说明见
`docs/SOLIDWORKS_BASE_MODELING_STABILITY_2026-09-01.md`。证据文件大小 18,619 字节，SHA-256 为
`1E0B861621EFB227734EA8295C56BA97E9744506F898F08DF9D40C3337BFFE2F`。

高级能力实机证据位于 `artifacts/live-validation/advanced-20260810-07/`。关键文件：

- `advanced-family.SLDPRT`
- `advanced-assembly.SLDASM`
- `advanced-assembly-bom.SLDDRW`
- `advanced-family-section.SLDDRW/.PDF/.DWG`
- `advanced-family-three-view.SLDDRW`
- `assembly-pmi-result.json`

## 3. C# Add-in 实机验收

- SolidWorks 自动调用 `ConnectToSW`，`SetAddinCallbackInfo2` 成功。
- Task Pane 在 SolidWorks 内创建并显示连接、活动文档、配置和选择数。
- 文档级宿主发布实时 `context.changed`，已观测零件和前视基准面持久引用。
- 25/25 项 Interop 声明经反射可见；桌面运行时还必须校验快照属于当前 Add-in 进程和连接时刻，反射结果不再表述为运行调用已成功。

## 4. 边界与未验证外部条件

- 当前只安装了 SolidWorks 2025 SP5.0；2024/2026 不能声称实机通过。
- PDM Professional COM 未安装，Document Manager 也需合法许可证密钥才能执行企业数据回归。
- 需增加真实带 PMI/DimXpert 的零件、大型装配体、轻化/压缩和缺引用夹具，才能扩展能力实验矩阵。
- PyInstaller one-folder、便携 ZIP 和 IExpress 单文件安装器已构建。EXE 冷启动进入 Qt 输入空闲并产生有效主窗口句柄；Setup 只解包、内部 ZIP 哈希、隔离安装/卸载和 Event Bridge 载荷验证通过。发布内容 210 个文件，数据库/日志/CAD/PDB/凭据计数为 0。
- Setup 未使用商业 Authenticode 证书，本地状态为 `NotSigned`；未在第二个干净 Windows 用户上完成 DPAPI 设置和全系统 Add-in 注册回归。

## 5. 基线封存

`backups/2026-08-10-v3.0.0-eventbridge-baseline/` 包含 107 个源文件的基线 ZIP、说明和 SHA-256 清单。ZIP 大小 184,721 字节，SHA-256 为 `73C4E169FC60A539C1CF4777FCB5374AD1C462193406DDB528E4B496BC9DDEDA`；不包含 `.git`、日志、数据库、密钥、EXE/DLL/PDB 或历史构建物。
