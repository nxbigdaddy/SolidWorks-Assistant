# SolidWorks 基础建模稳定性收口报告

验证日期：2026-09-01（Asia/Shanghai）<br>
目标环境：SolidWorks Premium 2025 SP5.0（修订号 33.5.0）<br>
实现边界：纯 Python、pywin32、官方 SolidWorks COM API

## 1. 收口结论

`src/sw_assistant` 的基础写入链路已完成本轮稳定性收口。最终实机矩阵在 14 个相互隔离的未保存零件中
全部通过，退出码为 0；共记录 14 个真实特征定义证据，`ISketch.CheckFeatureUse` 失败数为 0，
`IFeature.GetErrorCode2` 错误/警告数为 0，旋转选择 Mark 失败数为 0。所有测试零件的路径均为空，脚本按
精确标题关闭自己创建的文档，结束后没有残留 `SLDWORKS.exe`。

完整机器可读证据：`artifacts/live-solidworks-smoke-2026-09-01.json`，18,619 字节，SHA-256：
`1E0B861621EFB227734EA8295C56BA97E9744506F898F08DF9D40C3337BFFE2F`。

## 2. 本轮定位到的真实故障

| 故障 | 实机表现 | 根因 | 修复 |
| --- | --- | --- | --- |
| 活动草图身份读取失败 | `sketch.begin` 后提示无法取得草图特征名 | 当前 pywin32 包装下 `ISketch` 有 `Name` 和持久引用，但没有 `GetFeature` | 以 `ISketch.Name` 和 `GetPersistReference3(ISketch)` 建立会话身份；旧包装仅作兼容回退 |
| 特征定义读取类型不匹配 | 拉伸已经创建，随后 `AccessSelections` 抛出 COM 类型不匹配 | `None` 没有被封送成 API 所需的空 `IDispatch` | 显式传入 `VARIANT(VT_DISPATCH, None)`，并在 `finally` 中释放选择访问 |
| 已创建特征被识别成 `ICE` | 多级拉伸第 9 步无法判断 Boss/Cut | 开启 Instant3D 时 `GetTypeName2()` 返回包装类型 `ICE` | 遇到 `ICE` 时按官方约定调用 `GetTypeName()` 取得底层类型 |
| 面或中心线偶发失效 | 重建后卡在某个面或中心点附近 | 跨重建保存了生命周期已结束的 `Face2`/`ISketchSegment` COM 包装 | 只保存持久引用和几何签名；每次重建后从当前文档重新解析唯一对象 |
| 残留选择导致错误特征 | 同一计划偶发选错轮廓或中心线 | 选择集是 SolidWorks 的全局易变状态 | 写入前清空选择；旋转严格验证总数 2、草图 Mark=0 一个、中心线 Mark=4 一个 |
| COM 返回对象但模型仍不正确 | API 表面成功，后续步骤才失败 | 非空 COM 返回值不代表参数、拓扑或重建健康正确 | 读回特征定义、检查 `GetErrorCode2`、实体数和体积；不一致立即 `EditUndo2(1)` 并重建 |

## 3. 稳定执行链

1. 自然语言先转换成白名单 `ToolCall`，工具 schema 检查单位、范围、顺序和可写边界。
2. 独立 STA Worker 独占 COM 对象；项目自有 Windows 命名互斥锁覆盖前快照、写入和后快照。
3. `sketch.begin` 或 `sketch.begin_face` 绑定文档、配置和活动草图身份；任何漂移都在几何写入前失效。
4. 拉伸/旋转前调用 `ISketch.CheckFeatureUse`，开放、自交、零长度或中心线语义错误不会进入特征 API。
5. 旋转关闭草图并重建后，按持久引用或 ID/type/端点签名重新取得当前中心线，再验证 Mark 集合。
6. 特征创建后强制重建，读取 `IExtrudeFeatureData2`/`IRevolveFeatureData2`，并调用
   `IFeature.GetErrorCode2`。
7. 深度、角度、方向、终止条件、操作或合并状态不符时撤销最后特征；撤销失败则停止，绝不继续叠加写入。
8. 后验再核对草图、特征、实体、体积、面积和选择数；失败若改变拓扑，执行器禁止原样重试。

## 4. 实机矩阵

| 场景 | 关键验收 |
| --- | --- |
| `cylinder` | Front 圆、10 mm 盲拉伸、解析体积 |
| `plane-front` | Front 基准面冷启动重复性 |
| `plane-top` | Top 非 XY 坐标框架与体积 |
| `plane-right` | Right 非 XY 坐标框架与体积 |
| `shaft-m4` | 直径 4 mm、长度 30 mm 轴体 |
| `extrude-mid-plane` | 读回 `mid_plane` 和 8 mm 深度 |
| `extrude-reverse` | 读回 `reverse=true` 和 4 mm 深度 |
| `revolve-cylinder` | 360° Boss、持久中心线、Mark=0/4 |
| `revolve-reverse` | 180°、`reverse=true`、半体积 |
| `revolve-cut` | Boss 后 360° Cut，单实体体积严格下降 |
| `socket-head-m4x30` | 12 步轴体、两次端面重解析、头部和内六角切除 |
| `open-profile-rejected` | 开放轮廓在特征写入前拒绝，1 条草图线和 0 实体保持不变 |
| `rollback-mismatch` | 故意篡改定义读回，`EditUndo2(1)` 后恢复原特征数、0 实体、0 体积 |
| `context-drift` | 切换到第二个未保存零件后写入被拒绝，第二草图保持 0 实体 |

证据中的 14 个特征包括 10 个 Boss Extrude、1 个 Cut Extrude、2 个 Boss Revolve 和 1 个 Cut Revolve；
四种 `CheckFeatureUse` 用途均有实机覆盖。所有特征 `error_code=0`、`warning=false`。

### 4.1 用户故障复现实机回归

针对日志中“前视面六边形拉伸 90 mm，再在右视面局部 `(0, 90)` 创建六边形并拉伸 30 mm”的失败链，
新增 `hex-key-corner` 场景并连续独立执行 4 次。四次均为 8 步完成、退出码 0、最终 1 个实体、
0 个活动草图实体、22 个特征、体积 `3.6997510955268e-06 m³`；两次拉伸的
`CheckFeatureUse=0`、`GetErrorCode2=0`，两个正多边形均确认走 `solidworks-native` 后端。

机器可读证据：

| 文件 | SHA-256 |
| --- | --- |
| `artifacts/live-hex-key-corner-2026-09-01.json` | `D69BA33DE8B38FAF020BAD6F27561CED1FFF7A2D161AFF53C1A339CC98C9CD84` |
| `artifacts/live-hex-key-corner-repeat-1-2026-09-01.json` | `F1E5A4739EB7AC9C8FE167EA0DC6F249A4DCBDA2BE8A2636B3642DA1AD4FE4B6` |
| `artifacts/live-hex-key-corner-repeat-2-2026-09-01.json` | `F8EF78B3544339D59621840F1C4CBB8844FA3AE91B23455F34E701EC27FB89E9` |
| `artifacts/live-hex-key-corner-repeat-3-2026-09-01.json` | `DB45934759AF764466511159849F30901C8FA9B46D2B6323234A89FEFD8B4E8B` |

该场景验证的是互相垂直且合并为一个实体的六棱柱转角，不等同于带圆弧扫描/圆角的制造级弯折内六角扳手。

## 5. 自动化与独立性门禁

- `pytest`：204 项通过。
- Ruff：`launcher.py src scripts tests` 全部通过。
- mypy：56 个 `src` 源文件和实机冒烟脚本零问题。
- compileall：`launcher.py src scripts tests` 通过。
- 当前运行源码、脚本、测试、依赖清单中，01yx、Python.NET、`import clr`、`AddReference`、
  `LoadLibrary` 或显式 DLL 加载命中数为 0。
- 运行源码目录 DLL 数量为 0；项目目录现有 143 个 DLL 均来自既有构建/发布输出。与本机外部只读
  01yx 目录当前发现的 6 个 DLL 做 SHA-256 比对，相同哈希为 0；6 是 2026-09-01 本次复核快照。

因此，本项目没有复制、加载或依赖 01yx DLL。`old/` 中保留的文字分析和迁移参考不是运行入口，也不包含
被当前建模链加载的第三方函数库。

## 6. 重跑方法

```powershell
$env:PYTHONPATH="$PWD\src"
python scripts\live_solidworks_smoke.py `
  --scenario all `
  --result-json artifacts\live-solidworks-smoke-2026-09-01.json
```

`all` 不允许与 `--keep-open` 同时使用。每个场景独立启动/连接 SolidWorks，只创建未保存零件并在退出前
关闭；不得把生产 CAD 文件用于这套写入测试。

## 7. 边界

本报告收口的是当前白名单内的基础草图、拉伸、切除、旋转、端面重解析和失败保护。SolidWorks 2024/2026
等未安装版本、任意圆角/孔向导/放样/扫描/曲面，以及带真实 PMI、PDM 或 Document Manager 许可证的企业
夹具仍属于各自的版本/能力矩阵，不应从本报告推导为已验收。
