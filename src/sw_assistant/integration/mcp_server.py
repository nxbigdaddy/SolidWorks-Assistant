from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..infrastructure import AppPaths
from ..library import LibraryAnalyzer, LibraryRepository
from ..tools import build_tool_registry


class McpLibraryFacade:
    """Protocol-neutral, read-only surface used by the optional MCP server."""

    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.default()
        self.paths.ensure()
        self.repository = LibraryRepository(self.paths.library_db)
        self.analyzer = LibraryAnalyzer(self.repository)

    def search_library(
        self, query: str = "", limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 100))
        safe_offset = max(0, int(offset))
        return {
            "total": self.repository.document_count(query),
            "documents": [
                asdict(record)
                for record in self.repository.documents(query, safe_limit, safe_offset)
            ],
        }

    def document_manifest(self, document_id: int) -> dict[str, Any]:
        record = self.repository.document(int(document_id))
        if record is None:
            raise ValueError(f"模型库记录不存在：{document_id}")
        manifest = self.repository.manifest(record.id)
        if manifest is None:
            raise ValueError(f"{record.name} 尚未读取参数。")
        return {"document": asdict(record), "manifest": manifest}

    def quality_report(self, document_id: int) -> dict[str, Any]:
        return self.analyzer.quality_report(int(document_id))

    def dependency_graph(self, document_id: int) -> dict[str, Any]:
        return self.analyzer.dependency_graph(int(document_id))

    def similar_documents(self, document_id: int, limit: int = 10) -> list[dict[str, Any]]:
        return self.analyzer.similar(int(document_id), limit=limit)

    def compare_documents(self, left_id: int, right_id: int) -> dict[str, Any]:
        return self.analyzer.compare(int(left_id), int(right_id))

    @staticmethod
    def modeling_capabilities() -> list[dict[str, Any]]:
        return build_tool_registry().describe()


def create_server(paths: AppPaths | None = None):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            'MCP 可选依赖尚未安装；请运行 python -m pip install -e ".[mcp]"。'
        ) from exc

    facade = McpLibraryFacade(paths)
    server = FastMCP(
        "SolidWorks Model Library",
        instructions=(
            "只读访问本地 SolidWorks 模型库与参数分析。"
            "本服务不执行 SolidWorks 命令，也不修改 CAD 文件。"
        ),
        json_response=True,
    )

    @server.tool()
    def search_library(query: str = "", limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """按名称或路径检索本地 SolidWorks 模型库。"""
        return facade.search_library(query, limit, offset)

    @server.tool()
    def get_document_manifest(document_id: int) -> dict[str, Any]:
        """读取一个模型库文档最近保存的完整参数 Manifest。"""
        return facade.document_manifest(document_id)

    @server.tool()
    def inspect_document_quality(document_id: int) -> dict[str, Any]:
        """检查特征错误、缺失或重复引用以及质量属性状态。"""
        return facade.quality_report(document_id)

    @server.tool()
    def get_dependency_graph(document_id: int) -> dict[str, Any]:
        """读取装配体或工程图的本地引用关系图。"""
        return facade.dependency_graph(document_id)

    @server.tool()
    def find_similar_documents(document_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """按已读取的特征、尺寸名称、属性字段和材料查找相似模型。"""
        return facade.similar_documents(document_id, limit)

    @server.tool()
    def compare_documents(left_id: int, right_id: int) -> dict[str, Any]:
        """比较两个已读取参数的模型库文档。"""
        return facade.compare_documents(left_id, right_id)

    @server.tool()
    def get_modeling_capabilities() -> list[dict[str, Any]]:
        """读取桌面助手当前公开建模工具的参数约束。"""
        return facade.modeling_capabilities()

    return server


def main() -> int:
    create_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
