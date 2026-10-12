from __future__ import annotations

from sw_assistant.library import LibraryAnalyzer, LibraryRepository, compare_manifests


def _add(repository, root_id, path, document_type, manifest):
    path.write_bytes(document_type.encode("ascii"))
    document_id, _ = repository.upsert_document(
        root_id=root_id,
        path=str(path),
        document_type=document_type,
        size_bytes=path.stat().st_size,
        modified_ns=path.stat().st_mtime_ns,
        quick_fingerprint=path.name,
    )
    repository.set_manifest(document_id, manifest, f"manifest-{document_id}", f"file-{document_id}")
    return document_id


def test_manifest_comparison_reports_dimensions_properties_features_and_geometry():
    left = {
        "document_type": "part",
        "dimensions": [{"name": "D1@Sketch1", "display_value_mm": 10.0}],
        "custom_properties": {"PartNumber": "A", "Finish": "Black"},
        "features": [{"name": "Boss1", "type": "Boss"}],
        "geometry": {"body_count": 1, "volume_m3": 0.1},
    }
    right = {
        "document_type": "part",
        "dimensions": [
            {"name": "D1@Sketch1", "display_value_mm": 12.0},
            {"name": "D2@Sketch1", "display_value_mm": 5.0},
        ],
        "custom_properties": {"PartNumber": "B"},
        "features": [{"name": "Boss1", "type": "Boss"}, {"name": "Cut1", "type": "Cut"}],
        "geometry": {"body_count": 1, "volume_m3": 0.2},
    }

    result = compare_manifests(left, right)

    assert result["equal"] is False
    assert result["dimensions"]["changed"]["D1@Sketch1"]["after"] == 12.0
    assert result["dimensions"]["added"]["D2@Sketch1"] == 5.0
    assert "document:Finish" in result["properties"]["removed"]
    assert result["features"]["changed"] is True
    assert result["summary"]["geometry.volume_m3"]["after"] == 0.2


def test_dependency_graph_resolves_indexed_relative_reference_and_reports_missing(tmp_path):
    repository = LibraryRepository(tmp_path / "library.db")
    root_id = repository.add_root(str(tmp_path))
    part = tmp_path / "Part1.SLDPRT"
    assembly = tmp_path / "Assembly.SLDASM"
    part_id = _add(
        repository,
        root_id,
        part,
        "part",
        {"features": [], "dimensions": [], "geometry": {"mass_available": False}},
    )
    assembly_id = _add(
        repository,
        root_id,
        assembly,
        "assembly",
        {
            "features": [],
            "dimensions": [],
            "geometry": {"mass_available": True},
            "references": [
                {"name": "Part1-1", "path": "Part1.SLDPRT", "suppressed": False},
                {"name": "Missing-1", "path": "Missing.SLDPRT", "suppressed": False},
            ],
        },
    )
    analyzer = LibraryAnalyzer(repository)

    graph = analyzer.dependency_graph(assembly_id)
    report = analyzer.quality_report(assembly_id)

    assert {node["id"] for node in graph["nodes"]} == {assembly_id, part_id}
    assert any(edge["target_id"] == part_id for edge in graph["edges"])
    assert any(not edge["resolved"] for edge in graph["edges"])
    assert report["status"] == "error"
    assert any(item["code"] == "missing_reference" for item in report["issues"])


def test_similarity_uses_observed_features_dimensions_properties_and_material(tmp_path):
    repository = LibraryRepository(tmp_path / "library.db")
    root_id = repository.add_root(str(tmp_path))
    common = {
        "features": [{"type": "Boss"}, {"type": "Cut"}],
        "dimensions": [{"name": "D1@Sketch1"}, {"name": "D1@Boss1"}],
        "custom_properties": {"PartNumber": "P"},
        "material": {"name": "Steel"},
    }
    source_id = _add(repository, root_id, tmp_path / "Source.SLDPRT", "part", common)
    close_id = _add(repository, root_id, tmp_path / "Close.SLDPRT", "part", dict(common))
    far_id = _add(
        repository,
        root_id,
        tmp_path / "Far.SLDPRT",
        "part",
        {
            "features": [{"type": "Revolve"}],
            "dimensions": [{"name": "Angle@Revolve1"}],
            "custom_properties": {"Description": "Other"},
            "material": {"name": "Aluminum"},
        },
    )

    results = LibraryAnalyzer(repository).similar(source_id)

    assert results[0]["id"] == close_id
    scores = {item["id"]: item["score"] for item in results}
    assert scores[close_id] > scores[far_id]
