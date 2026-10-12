# 开发与验收操作指南

## 1. 首次启动

在工程根目录执行 `python -m pip install -e ".[dev]"`，随后运行 `python -m sw_assistant` 或双击
`run.bat`。数据写入 `%LOCALAPPDATA%\SolidWorksAIAssistant`，工程目录不保存运行凭据和用户数据库。
若根目录存在 `old/` 且尚未迁移，启动后会在后台只读导入旧模型库和历史。

## 2. 日常源码门禁

```powershell
python -m ruff check launcher.py src scripts tests
python -m mypy src
python -m compileall -q launcher.py src scripts tests
python -m pytest -q
```

测试固定使用工程内 `.pytest_tmp`。本机第三方 pytest 插件产生的依赖警告要与本项目失败分开判断，
但任何非零退出码都必须处理。

## 3. SolidWorks 验收

先关闭未保存的重要文档，复制夹具到临时目录，再依次验证连接、新建零件、三基准面、圆、矩形、凸台、
切除、失败恢复、停止、深度读取和参数派生。每个夹具记录 SolidWorks 主版本、语言、模板路径、操作前后
快照和源文件 SHA-256。不得把生产 CAD 文件直接作为写测试目标。
新增验收还要覆盖旋转中心线选择标记、产品族配置重开、持久引用配合、装配体 Pack and Go、
工程图视图/BOM/气泡、交换格式导出、PMI/DFM、缺失引用质量检查和命名目标在拓扑变化后的明确失效提示。

基础圆柱冒烟可执行：

```powershell
$env:PYTHONPATH="$PWD\src"
python scripts\live_solidworks_smoke.py
```

M4×30 内六角圆柱头螺钉全流程冒烟可执行：

```powershell
$env:PYTHONPATH="$PWD\src"
python scripts\live_solidworks_smoke.py --scenario socket-head-m4x30
```

基础建模完整稳定性矩阵可执行：

```powershell
$env:PYTHONPATH="$PWD\src"
python scripts\live_solidworks_smoke.py `
  --scenario all `
  --result-json artifacts\live-solidworks-smoke-2026-09-01.json
```

矩阵覆盖 14 个隔离场景：三基准面、盲拉伸、中面、反向拉伸、旋转凸台/切除、反向旋转、多级端面建模、
开放轮廓拒绝、定义不一致撤销和跨文档漂移。脚本核对最终体积或切除前后体积、特征定义、
`CheckFeatureUse`、`GetErrorCode2` 及旋转 Mark 数量；默认关闭且不保存它创建的测试零件。只有单场景人工
检查时才使用 `--keep-open`，`all` 明确禁止保持文档打开。

12 项能力的本机冒烟脚本包括：

```powershell
python scripts\live_advanced_workflows_smoke.py --output <output-directory>
python scripts\live_assembly_pmi_smoke.py <assembly-path> <result-json>
python scripts\live_mate_smoke.py <assembly-path> --mate-type parallel
python scripts\live_drawing_smoke.py <part-path> <drawing-template> <drawing-path> --three-view --section
python scripts\live_export_smoke.py <drawing-path> <pdf-output> <dwg-output>
powershell -ExecutionPolicy Bypass -File scripts\build_event_bridge.ps1
```

配合脚本创建特征后立即撤销并不保存关闭，再次核对源装配体哈希。其他写测试必须输出到专用夹具目录。

## 4. 发布

用户明确要求发布时执行 `powershell -ExecutionPolicy Bypass -File .\build.ps1`。脚本运行静态检查、测试和
PyInstaller，并审计发布目录不得包含数据库、日志或 CAD 文件。构建成功仍需在干净 Windows 用户下做
EXE 冷启动、DPAPI 设置、COM spawn、中文字体和帮助文档检查；完成这些步骤后才可打包 ZIP。

## 5. 回退

代码回退使用 Git。重构前程序与数据快照位于 `old/`，其清单在 `old/ARCHIVE-MANIFEST.md`。
12 项迭代修改前的独立源码包位于 `backups/2026-08-10-v3.0.0-eventbridge-baseline/`，回退前先核对 `MANIFEST.sha256`。不要让新旧
程序同时写同一用户数据库。迁移器只读旧库，因此删除新用户数据目录不会破坏归档；执行删除前仍应先
备份并得到用户授权。
