# Agent 版迁移与回退指南

## 1. 目录关系

- 旧项目：`<workspace>/solidworks_tools`
- Agent 新版本：`<workspace>/solidworks_tools_agent`

两个目录彼此独立。本次迭代没有在旧目录中覆盖文件，也没有把旧目录的 `.git`、缓存、构建产物、
备份 ZIP 或用户数据复制到新版本。新目录最初只复制源码、测试、脚本、安装器源码和文档，再加入
Agent 相关实现。

用户数据仍默认位于 `%LOCALAPPDATA%\SolidWorksAIAssistant`。因此两个版本不能假设可以同时写同一
份设置、SQLite 或审计日志；切换版本前应先退出正在运行的另一个版本。

## 2. 首次建立环境

```powershell
cd <workspace>/solidworks_tools_agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,build]"
```

必须使用不带 `--system-site-packages` 的干净虚拟环境。否则 PyInstaller 可能把全局 Python 中的
IPython、numpy、Python.NET/CLR 等无关依赖误收进发布分析，导致体积膨胀和不可审计的 DLL。

## 3. 启动与设置

```powershell
.\run.bat
```

设置窗口包含：

- “启用需求规划 + 失败恢复的全流程 Agent”：默认开启；
- “单次请求上限”：默认 6，可选 2–8；
- “失败恢复后端”：默认“自动（优先 Pi，失败时回退）”；
- “Pi 修正预算”：默认每个任务 6 轮，可选 2–12。

原模型地址、模型名、超时和 DPAPI API Key 设置继续复用。旧设置文件缺少新增字段时会自动使用
默认值，不需要手工编辑 JSON。

若兼容端点不支持结构化工具调用，可先关闭全流程 Agent。关闭后仍使用旧版受限 `PlannerService`
规划/修复流程，不会禁用单步校验、快照、回滚或手工恢复。

Pi 后端需要 Node.js 22.19+ 与 `@earendil-works/pi-coding-agent@0.84.4`。项目不复制 Pi GUI，
也不读取用户全局 Pi 设置、扩展或凭据；运行时会使用应用自己的隔离配置目录。未安装 Pi 时自动模式
会明确记录回退并继续使用内置监督器。可用下面命令安装本版本验证过的开源依赖：

```powershell
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.84.4
```

## 4. 发布前门禁

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m mypy src launcher.py
.\.venv\Scripts\python.exe -m compileall -q src launcher.py
.\.venv\Scripts\python.exe -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

构建产物位于 `dist\SolidWorksAssistant`。发布前至少验证：

1. EXE 可以冷启动并保持运行；
2. 发布目录没有 `01yx`、`OneYx`、`pythonnet` 或 `clr` 命中；
3. Agent 的本地假 OpenAI 端点三轮工具调用测试通过；
4. 真实 SolidWorks 测试仅操作脚本新建、未保存的夹具；
5. 未经明确授权，不向外部模型端点发送真实日志或 CAD 上下文。

## 5. 回退方法

最简单的回退是不修改新目录，而是：

1. 退出 Agent 版和 SolidWorks；
2. 从 `<workspace>/solidworks_tools` 启动旧版本；
3. 若设置兼容端点导致旧版异常，在旧版设置中重新保存模型配置；
4. 不要把 Agent 版的 `src/sw_assistant/agent` 复制回旧项目。

如果只想回退 Agent 行为而保留 v4 其他修复，可在设置中关闭全流程 Agent。该开关是可逆的，不删除
历史、设置或模型库。

## 6. 数据和二进制边界

- 不从 01yx 目录复制 DLL、配置、模型、提示词或反编译代码；
- Python SolidWorks 写入继续使用 pywin32，不依赖 Python.NET；
- `src-dotnet` 中的 Event Bridge 是本项目自有可选 Add-in，安装器单独构建和注册；
- `.venv`、`build`、`dist`、缓存和本地实机证据不应提交为源码；
- API Key、DPAPI 密文、SQLite、JSONL、日志和用户 CAD 文件不得进入发布源码包。

## 7. 目标机器验收顺序

1. 在不启动 SolidWorks 的情况下完成纯测试与 EXE 冷启动；
2. 检查 SolidWorks 版本、模板、语言、Interop 和可选 Add-in；
3. 运行只创建未保存零件的基础建模冒烟；
4. 使用本地假端点验证 Agent 协议；
5. 获得数据传输授权后，才使用真实模型端点测试一条可控失败；
6. 人工检查审计记录、替代步骤和最终几何，再决定是否扩大试点。
