# 构建、发布与运维

## 1. 开发

Python 3.11+，使用 `pip install -e .[dev]`。运行 `python -m sw_assistant`。测试临时目录固定在工程内，
避免 Windows 默认临时目录权限差异。

## 2. 构建

`build.ps1` 依次执行代码检查、测试和 PyInstaller。发布采用 one-folder，保留 Qt 与 pywin32 依赖目录。
帮助文档复制到 `Help/`。构建脚本不得读取或复制 `%LOCALAPPDATA%` 数据。
PyInstaller 以根目录 `launcher.py` 为入口；不要直接把包内 `__main__.py` 当普通脚本执行，否则相对导入
语义与 `python -m sw_assistant` 不一致。

C# 事件桥使用 `.NET Framework 4.8 / x64`，执行 `scripts\build_event_bridge.ps1` 编译并运行其自包含
测试。管理员 PowerShell 使用 `scripts\register_event_bridge.ps1` 注册；启动 SolidWorks 后运行
`scripts\check_event_bridge.ps1` 检查连接、心跳和能力快照。正式发布前必须配置强名称签名。

用户要求 Windows 安装程序时执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

该脚本先生成 PyInstaller one-folder 和 Event Bridge，再使用 Windows IExpress 生成单文件 Setup EXE，同时产生便携 ZIP、SHA-256 和发布 manifest。安装器验证内部 ZIP 哈希，安装到 Program Files，建立快捷方式和卸载项，并使用 64 位 RegAsm 注册 Add-in。用户数据目录不会进入安装包，卸载时也会保留。

## 3. 诊断

“帮助 → 显示日志位置”定位 JSONL。规划、执行和库任务记录请求或任务 ID。应用启动时按照
“日志保留天数”清理过期的 `session-*.jsonl`，不会触碰其他文件；清理结果和失败也写入新会话审计。
长时间 COM 无响应时中断自动化工作进程并重连，不直接结束 SolidWorks。

## 4. 备份

备份用户数据目录中的设置、模型库和历史；API Key 密文只在同一 Windows 用户配置下可解密。CAD 源文件
仍由用户自己的文件管理/PDM 备份。

## 5. 版本

应用、数据库 schema、计划协议和 Manifest 分别版本化。小版本保证数据向前迁移；重大协议变化保留
只读导出工具。
