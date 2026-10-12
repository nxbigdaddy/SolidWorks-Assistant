# MCP 只读集成

## 范围

可选 MCP 服务只暴露本地模型库的只读能力：检索文档、读取 Manifest、质量检查、依赖图、相似模型、
参数差异以及桌面助手公开工具的 schema。它不连接 SolidWorks、不执行计划、不修改 CAD 文件，也不读取
模型服务 API Key。C# Add-in、SolidWorks 事件桥和写操作授权不属于这一阶段。

## 安装与启动

MCP SDK 是可选依赖，不影响桌面程序：

```powershell
python -m pip install -e ".[mcp]"
solidworks-library-mcp
```

传输使用标准输入输出。服务自身不得向 stdout 写日志，否则会破坏 JSON-RPC 数据；客户端配置时应把
命令指向项目虚拟环境中的 `solidworks-library-mcp.exe`，或使用该虚拟环境 Python 执行
`-m sw_assistant.integration.mcp_server`。

## 工具

- `search_library`
- `get_document_manifest`
- `inspect_document_quality`
- `get_dependency_graph`
- `find_similar_documents`
- `compare_documents`
- `get_modeling_capabilities`

所有文档参数都来自本地数据库最近一次成功快照，可能落后于磁盘文件。调用方必须检查
`parameter_status`、`file_sha256` 和 `updated_at`，不能把相似度当作几何等价证明。
