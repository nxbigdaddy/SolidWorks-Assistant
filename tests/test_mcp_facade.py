from __future__ import annotations

import pytest

from sw_assistant.infrastructure import AppPaths
from sw_assistant.integration import McpLibraryFacade


def _paths(tmp_path):
    return AppPaths(
        data=tmp_path,
        logs=tmp_path / "logs",
        settings=tmp_path / "settings.json",
        library_db=tmp_path / "library.db",
        history_db=tmp_path / "history.db",
    )


def test_mcp_facade_is_read_only_search_manifest_and_analysis_surface(tmp_path):
    facade = McpLibraryFacade(_paths(tmp_path))
    root_id = facade.repository.add_root(str(tmp_path))
    source = tmp_path / "Part1.SLDPRT"
    source.write_bytes(b"part")
    document_id, _ = facade.repository.upsert_document(
        root_id=root_id,
        path=str(source),
        document_type="part",
        size_bytes=4,
        modified_ns=source.stat().st_mtime_ns,
        quick_fingerprint="quick",
    )
    manifest = {
        "schema_version": 2,
        "features": [{"name": "Boss1", "type": "Boss", "error_code": 0}],
        "dimensions": [{"name": "D1@Sketch1", "display_value_mm": 20}],
        "geometry": {"mass_available": False},
    }
    facade.repository.set_manifest(document_id, manifest, "manifest", "file")

    search = facade.search_library("Part1")

    assert search["total"] == 1
    assert search["documents"][0]["id"] == document_id
    assert facade.document_manifest(document_id)["manifest"] == manifest
    assert facade.quality_report(document_id)["status"] == "ok"
    assert any(
        tool["name"] == "feature.revolve" for tool in facade.modeling_capabilities()
    )


def test_mcp_facade_rejects_unknown_or_unread_document(tmp_path):
    facade = McpLibraryFacade(_paths(tmp_path))
    with pytest.raises(ValueError, match="不存在"):
        facade.document_manifest(999)
