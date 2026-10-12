# SolidWorks 集成设计

## 1. 自动化方式

使用官方 SolidWorks COM Automation API。应用通过 `SldWorks.Application` 连接或启动实例，
不模拟鼠标键盘。COM 初始化、对象创建和释放全部在独立工作进程的主线程完成。

## 2. 文档与选择

- 新零件优先使用配置模板调用 `NewDocument`。
- 基准面按内部候选名、当前语言显示名和特征类型匹配，不能只依赖中文名称。
- 新建草图使用 `SketchManager.InsertSketch(True)`。
- 拉伸后从特征返回面中按草图法向和端面距离解析最外侧平面，下一草图通过面对象选择进入；不使用易变的
  “第几个面”。
- 小尺寸圆和直线若在自动关系模式下返回空对象，清除选择并临时启用 `SketchManager.AddToDB` 后按精确
  坐标重试，随后恢复原属性并通过草图实体数验证。
- 工具执行前清空无关选择；依赖当前选择的工具必须声明并验证选择数。
- 拓扑对象不能仅靠面序号长期引用；当前可把唯一选择通过 `GetPersistReference3` 保存为命名目标，并用
  `GetObjectByPersistReference3` 在同一文档恢复。拓扑变化导致引用失效时明确暂停，不猜测替代面。
- 自动建模步骤也不再跨重建缓存 `Face2`。拉伸端面保存为文档/配置、来源特征、中心、法向、面积、包围盒
  和可选持久引用；下一草图先重建，再通过持久引用或当前特征面集合重新解析，且只接受唯一匹配。
- 活动草图身份取自 `ISketch.Name` 和草图持久引用，不假设 pywin32 一定暴露 `ISketch.GetFeature`。
- 旋转前精确验证草图 Mark=0、中心线 Mark=4 和选择总数；中心线在重建后按持久引用或几何签名重新取得。

## 3. 单位

用户和计划协议采用毫米；COM 长度使用米。角度界面使用度，COM 使用弧度。转换函数集中在
边界层，领域模型禁止隐式单位。

## 4. 快照与后验条件

快照包含连接状态、文档标题/路径/类型、活动草图、草图实体数、特征数、实体数、体积、选择数
和采集时间。连接、新建、选择、草图、几何和特征工具分别定义不同的后验条件。

API 返回值只是一项证据。例如拉伸返回对象但实体数和体积均未按预期变化时，步骤仍失败。拉伸/旋转还会
读取实际特征定义并检查 `IFeature.GetErrorCode2`；Instant3D 返回 `ICE` 时读取底层类型。参数不符或存在
特征错误/警告时撤销最后特征并重建。

## 5. 进程故障

主进程为每次调用设置软超时并持续更新“SolidWorks 仍在处理”。停止只阻止后续调用。若 COM
工作进程无法响应，用户可断开工作进程；应用不结束用户的 SolidWorks 进程。新工作进程必须
重新连接并重新采集状态，不能复用旧 COM 引用。

## 6. 第一批工具

`session.ensure`、`document.new_part`、`selection.plane`、`sketch.begin`、`sketch.circle`、
`sketch.begin_face`、`sketch.rectangle`、`sketch.polygon`、`sketch.line`、`sketch.centerline`、
`feature.extrude`、`feature.revolve`、
`model.inspect`、`document.open_readonly`。

装配体复制使用 `assembly.pack_and_go` 内部工具：从文档扩展对象获取官方 Pack and Go 会话，设置目标
目录与工程图、Toolbox 引用选项，调用 `SavePackAndGo` 并检查所有返回状态。应用服务随后对临时输出做
文件级验证再发布，不能用文件系统直接复制 SLDASM 代替。

格式导出使用 `document.export` 内部工具和 `IModelDocExtension.SaveAs3`。零件/装配体允许
STEP/STL/IGES，工程图允许 PDF/DXF/DWG。导出前把文档设为活动文档、清空选择并拒绝包含未保存修改
的已打开源文档；应用层写唯一临时文件，检查非空和源哈希后才发布。

## 7. Document Manager 可选通道

用户配置合法的 SolidWorks Document Manager 许可证密钥后，可在不启动完整 SolidWorks 的情况下读取
配置、自定义属性和外部引用。该结果标记为 `metadata_ready`，不冒充包含特征、尺寸和质量属性的完整
COM 深度快照；“深度读取全部参数”仍使用独立 SolidWorks COM 工作进程。

## 8. C# Add-in 事件桥

`src-dotnet/SolidWorksAssistant.EventBridge` 是只读事件与能力探测侧车。它通过 `ISwAddin` 接入
SolidWorks，使用自动分派类接口向 `SetAddinCallbackInfo2` 提供回调对象，并订阅应用级文档、命令和
空闲事件。事件异步发布到 JSONL 和可选命名管道，不能在 SolidWorks 回调线程执行模型调用或磁盘扫描。

Python 主程序从 JSONL 当前末尾开始监听，在诊断页显示事件与能力摘要。收到 Add-in 事件后若要驱动
执行，必须交给既有 STA COM Worker 重新读取状态；详见 `EVENT_BRIDGE.md`。

## 9. 独立性约束

SolidWorks 建模路径只使用 Python、pywin32 和官方 SolidWorks COM API，不加载第三方建模 DLL，也不通过
Python.NET 执行外部函数库。跨进程串行锁、拓扑签名、特征定义回读和回滚均由本项目源码实现，详见
`STABLE_SOLIDWORKS_AUTOMATION.md`。
