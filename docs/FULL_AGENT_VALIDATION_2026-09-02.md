# v4.1 全流程 Agent 验证报告

验证日期：2026-09-03（Asia/Shanghai）<br>
项目：`<workspace>/solidworks_tools_agent`<br>
产品版本：`4.1.2`

## 1. 验收结论

v4.1 已把 Agent 从“执行失败后给恢复建议”扩展到“需求理解、工具检查、主动假设、计划生成、同会话
校验纠错、执行失败恢复”的完整受约束链路。模型没有 Shell、文件、DLL、动态代码、直接 COM 或保存
工具；所有 SolidWorks 写入仍经过本地 `ToolRegistry`、串行 COM Worker、快照、实际定义读回、几何
后验和失败回滚。

本轮源码门禁、打包、冷启动、DLL 独立性与真实 SolidWorks 隔离冒烟均通过。安装程序没有商业代码
签名。官方 DeepSeek 端点只做了不含用户 CAD/日志/路径的合成计划探针，真实故障恢复不能表述为已验收。

## 2. 最新手机任务回归

回归请求为“画一个 iPhone 17 Pro Max 手机模型”。受控 PydanticAI `TestModel` 返回并经本地策略授权：

- 6 步计划：连接、新建零件、前视基准面、开始草图、160 × 78 mm 中心矩形、9 mm 拉伸；
- 假设 1：160 × 78 × 9 mm 是近似包络，不宣称为官方精确规格；
- 假设 2：当前白名单没有圆角、按键、接口和摄像头细节，只建立无圆角主体；
- 没有再次要求用户补充普通外形尺寸；计划预览会显示上述假设；
- 对“与现有轴精密配合的孔”，仍会询问轴径和期望间隙，因为静默猜测会改变装配结果。

另一个本地 `127.0.0.1` OpenAI-compatible 假服务完成 3 轮 `/chat/completions`：需求检查、工具检查、
结构化最终结果。测试确认模型可见的工具只有三个只读检查工具和 `final_result`；API Key 只进入
Authorization 头。没有向公网或第三方发送数据。

## 3. 偶发草图失败的定位与修复

发布前重复执行 `hex-key-corner` 场景时，复现了第 4/8 步 `sketch.polygon` 后验失败：
`CreatePolygon` 返回了非空 COM 代理对象，但活动草图实体数没有增加。旧代码把“返回对象”误当作
“已提交”，导致外层快照才发现失败。

修复后的命令语义：

1. 记录调用前草图段数量；
2. 原生 `CreatePolygon` 后重读草图段；不足时执行重绘并再次读取，再执行有界重建并读取；
3. 只有观察到至少指定边数的实体增长才报告成功；
4. 确认零增长时允许一次原生重试；部分增长先精确清除本命令新增实体；
5. 两次原生调用都未提交时，进入逐边原子后备；后备也必须读回完整实体数，否则回滚并停止；
6. 草图会话漂移门禁没有放宽，只把文档路径、标题或配置的失配原因细分到错误信息中。

新增纯测试覆盖“首次原生返回未提交、第二次提交”和“原生始终未提交、逐边后备成功”。修复后在
SolidWorks Premium 2025 SP5.0 中连续运行 3 次；三次均为 8 步、退出码 0、1 个实体、无残留活动
草图，最终体积约 `3.699751095526799e-06 m³`。有详细提交证据的后两轮中，两次正六边形均使用
`solidworks-native`，首次提交观察到 7 个草图段；两个拉伸的 `CheckFeatureUse=0`、
`GetErrorCode2=0`、无警告。

实机证据：

- `artifacts/live-full-agent-hex-key-corner-fixed1-2026-09-02.json`<br>
  SHA-256 `3BDCC0D90898505F8E5E5368A1FACE29734B06AD068B304EE8DF3CD670F7D381`
- `artifacts/live-full-agent-hex-key-corner-fixed2-2026-09-02.json`<br>
  SHA-256 `5C9D351DCFF2242736E09420771B507AEDF533D840C2B716FBE36680FF86FA3A`
- `artifacts/live-full-agent-hex-key-corner-fixed3-2026-09-02.json`<br>
  SHA-256 `23C6731A4C6C55B374168FE59A9EB958DF00761228F62528EAFBF33C3A5EA022`

全部夹具均由脚本新建且未保存；没有修改用户 CAD 文件。

## 4. 自动化和构建门禁

| 门禁 | 结果 |
| --- | --- |
| `ruff check .` | 通过 |
| `mypy src` | 65 个源码文件零问题 |
| `compileall -q src tests scripts` | 通过 |
| `pytest -q` | 234 项通过，21.55 秒 |
| Event Bridge Release | 0 警告、0 错误，8/8 测试通过 |
| PyInstaller one-folder | 构建成功 |
| 打包 EXE 冷启动 | 隐藏启动 8 秒保持运行，随后正常关闭 |

234 项测试包括需求规划 Agent、监督 Agent、Provider 兼容层、打包运行时门禁、工具 Schema、执行状态机、拓扑稳定性、多边形提交恢复、
本地三轮假端点、设置迁移、假设预览和既有 UI/模型库/高级能力回归。

## 5. 发行产物

目录：`artifacts/release/4.1.2`

| 产物 | SHA-256 |
| --- | --- |
| `SolidWorksAssistant-4.1.2-Setup.exe` | 见同目录封装后生成的 `SHA256SUMS.txt` |
| `SolidWorksAssistant-4.1.2-portable.zip` | 见同目录封装后生成的 `SHA256SUMS.txt` |
| `dist/SolidWorksAssistant/SolidWorksAssistant.exe` | `B3AC01378AE4812CD21519F47B3427B2EF7FB7DD1B119166021A77878FD61590` |
| `dist/SolidWorksAssistant/EventBridge/SolidWorksAssistant.EventBridge.dll` | `61FF6BE578E83A243E0552392A69766DBCA98DC77DBE574E86A59676D29E6AD0` |

EXE 文件版本和产品版本均为 `4.1.2`。Setup 的 Authenticode 状态为 `NotSigned`，因此不能宣称为已签名
商业发布包。

## 5.1 QtCore 启动故障修复

用户报告的 traceback 来自 `build/SolidWorksAssistant/SolidWorksAssistant.exe`。该文件是
PyInstaller 在 `COLLECT` 之前生成的中间 EXE，目录只有 16 个文件，没有 `_internal`、`QtCore.pyd`
或 `Qt6Core.dll`；它与正式 EXE 名称和哈希相同，因此容易被误认为可交付程序。

4.1.1 增加了中间 EXE 与目录完整性保护；4.1.2 又补齐正式包启动链：

- 启动器在导入 PySide6 前验证 10 个关键 Python/Qt 文件；目录不完整时显示明确中文说明，不再输出
  `ImportError: DLL load failed while importing QtCore` traceback；
- `build.ps1` 在正式 `COLLECT` 完成后校验运行目录并删除 `build` 中间 EXE；
- 发布构建与安装脚本在压缩、安装替换之前再次检查 Python DLL、shiboken、QtCore/Gui/Widgets 和
  `qwindows.dll`，缺少任何一个都会停止，保留原安装。
- PyInstaller 解析 PATH 时曾误收集 Codex/Poppler 的 ICU 78 `icuuc.dll`，它遮蔽 Windows System32
  的无版本 ICU 转发 DLL，使 Qt6Core 因缺少入口点 127 失败；spec 现显式排除该外来 DLL，三层脚本
  同时拒绝含冲突文件的载荷。
- PydanticAI 启动时读取 `genai_prices` 与 `pydantic_ai_slim` 的发行元数据；spec 现显式复制两者，
  冷启动不再出现 `No package metadata was found`。
- 中间 EXE 删除增加 10 次有限重试，处理 PyInstaller/安全扫描刚释放文件时的短暂占用；最终仍删不掉
  会继续阻断发布。

验证结果：正式 `dist` 有 389 个文件并冷启动 8 秒；使用 4.1.2 安装载荷生成的隔离安装目录有
392 个文件，`QtCore.pyd` / `Qt6Core.dll` 与两份 Agent 元数据均存在并冷启动 8 秒；冲突 ICU 和
`build` 中间 EXE 均不存在。

## 6. 01yx 与运行时独立性

- `dist/SolidWorksAssistant`：389 个文件，其中 106 个 DLL；
- portable ZIP：393 个条目；
- 本机 `C:\Users\<user>\AppData\Local\Programs\01yx`：本轮发现 6 个 DLL；
- 本程序 106 个 DLL 与上述 6 个 DLL 做 SHA-256 逐项比对：0 个相同；
- 发行目录和 ZIP 中 `01yx` / `OneYx` 名称命中：0；
- `src` / `src-dotnet` 运行代码的 `pythonnet`、`import clr`、`AddReference`、`LoadLibrary`、
  `01yx` / `OneYx` 命中：0。

Event Bridge 是本项目源码自行构建的 .NET Framework 4.8 x64 Add-in，不是 01yx 二进制。

## 7. 尚未验收的外部条件

- 使用用户已配置的官方 DeepSeek 端点完成了合成 10 × 10 × 10 mm 长方体计划探针；只发送需求与
  工具 Schema，未发送日志、CAD 内容、路径或故障上下文，且没有执行 SolidWorks。真实故障恢复仍未验收。
- 当前只对 SolidWorks 2025 SP5.0 做了本轮实机验证，不能外推为 2024/2026 已通过。
- 手机模型仍受当前白名单限制，是可审查的近似包络，不是官方外形、曲面、圆角和细节复刻。
- Setup 未签名；目标机器部署前仍应按组织策略签名，并做目标 SolidWorks 版本的未保存夹具回归。
