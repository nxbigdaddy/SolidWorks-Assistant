from __future__ import annotations

import threading

import pytest

from sw_assistant.advanced import (
    AssemblyAssistant,
    AssemblyComponent,
    AssemblyReport,
    CapabilityDefinition,
    CapabilityLab,
    CapabilityState,
    CapabilityStatus,
    DemonstrationService,
    DrawingAutomationService,
    DrawingSpec,
    DrawingViewSpec,
    ManufacturingAnalyzer,
    MateRequest,
    PdmDocumentInfo,
    PdmIntegrationService,
    ProductFamilyService,
    ProductFamilySpec,
    FamilyVariant,
    WorkerProductFamilyAdapter,
    WorkerAssemblyAdapter,
    WorkerDrawingAdapter,
    WorkerPmiAdapter,
)
from sw_assistant.domain import ModelSnapshot, StepResult, ToolCall
from sw_assistant.library import LibraryRepository
from sw_assistant.solidworks.gateway import SolidWorksGateway


def _available(capability_id: str) -> CapabilityStatus:
    return CapabilityStatus(
        capability_id,
        CapabilityState.AVAILABLE,
        "fake adapter",
        source="test",
    )


def test_assembly_assistant_gates_and_verifies_mates(tmp_path):
    source = tmp_path / "fixture.SLDASM"
    source.write_bytes(b"assembly")

    class Adapter:
        def capability(self, capability_id):
            return _available(capability_id)

        def inspect_assembly(self, path, *, cancel: threading.Event):
            return AssemblyReport(path, (AssemblyComponent("Base", "base.SLDPRT"),))

        def create_mate(self, request, *, cancel: threading.Event):
            return {"created": True, "error_code": 0, "mate": request.mate_type}

    assistant = AssemblyAssistant(Adapter())
    assert assistant.inspect(str(source)).components[0].name == "Base"
    request = MateRequest(str(source), "persistent-a", "persistent-b", "coincident")
    assert assistant.create_mate(request)["mate"] == "coincident"
    with pytest.raises(ValueError, match="不能相同"):
        MateRequest(str(source), "same", "same", "coincident").validate()


def test_drawing_service_validates_parent_views_and_bom(tmp_path):
    model = tmp_path / "fixture.SLDASM"
    template = tmp_path / "a4.DRWDOT"
    destination = tmp_path / "fixture.SLDDRW"
    model.write_bytes(b"assembly")
    template.write_bytes(b"template")
    spec = DrawingSpec(
        str(model),
        str(destination),
        str(template),
        (
            DrawingViewSpec("主视图", "front"),
            DrawingViewSpec("俯视图", "projected", projected_from="主视图"),
        ),
        include_bom=True,
        include_balloons=True,
    )

    class Adapter:
        def capability(self, capability_id):
            return _available(capability_id)

        def generate_drawing(self, value, *, cancel: threading.Event):
            return {
                "destination": value.destination,
                "views": [item.name for item in value.views],
                "bom_created": True,
            }

    result = DrawingAutomationService(Adapter()).generate(spec)
    assert result["bom_created"] is True
    bad = DrawingSpec(
        str(model),
        str(destination),
        str(template),
        (DrawingViewSpec("投影视图", "projected", projected_from="不存在"),),
    )
    with pytest.raises(ValueError, match="父视图"):
        bad.validate()


def test_manufacturing_analyzer_falls_back_to_local_dfm_rules(tmp_path):
    source = tmp_path / "fixture.SLDPRT"
    report = ManufacturingAnalyzer().analyze(
        str(source),
        {"material": ""},
        metrics={
            "minimum_wall_mm": 0.4,
            "wall_limit_mm": 0.8,
            "maximum_hole_depth_mm": 100,
            "minimum_hole_diameter_mm": 5,
        },
    )
    assert report.capability.state is CapabilityState.UNAVAILABLE
    assert {item.rule_id for item in report.issues} >= {
        "dfm.thin_wall",
        "dfm.deep_hole_ratio",
        "dfm.material_missing",
    }


def test_pdm_service_is_explicitly_read_only_and_capability_gated(tmp_path):
    source = tmp_path / "fixture.SLDPRT"

    class Adapter:
        def capability(self, capability_id):
            return _available(capability_id)

        def inspect(self, path, *, cancel: threading.Event):
            return PdmDocumentInfo(path, vault="Engineering", version="7")

    service = PdmIntegrationService(Adapter())
    assert service.inspect(str(source)).version == "7"
    assert not hasattr(service, "check_out")


def test_demonstration_requires_review_before_contextual_use(tmp_path):
    service = DemonstrationService(LibraryRepository(tmp_path / "library.db"))
    session = service.start("part", context={"selection_types": [2], "document_type": "part"})
    service.record_action(
        session,
        command_id=100,
        command_name="Extrude",
        before={"feature_count": 1},
        after={"feature_count": 2},
        result={"reason": 0},
    )
    service.finish(session)
    recipe = service.compile_recipe(session, "拉伸演示")
    assert service.applicable(recipe, {"document_type": "part", "selection_types": [2]})[0] is False
    approved = service.approve(recipe.id)
    assert approved.steps[0]["context_delta"]["feature_count"]["after"] == 2
    assert (
        service.applicable(approved, {"document_type": "part", "selection_types": [2, 3]})[0]
        is True
    )
    replay = service.prepare_replay(
        approved.id, {"document_type": "part", "selection_types": [2, 3]}
    )
    assert replay["mode"] == "guided"
    assert replay["manual_confirmation_required"] is True
    assert replay["steps"][0]["target_confirmation_required"] is True
    recorded = service.record_replay_result(approved.id, succeeded=True)
    assert recorded.success_count == 1
    assert recorded.failure_count == 0


def test_default_capability_matrix_includes_the_version_lab(tmp_path):
    repository = LibraryRepository(tmp_path / "matrix.db")
    assert "capability.lab" in CapabilityLab(repository).definitions


def test_capability_lab_records_evidence_and_release_gate(tmp_path):
    repository = LibraryRepository(tmp_path / "library.db")
    lab = CapabilityLab(
        repository,
        (
            CapabilityDefinition("required", "core", "required", True),
            CapabilityDefinition("optional", "extra", "optional", False),
        ),
    )
    statuses = lab.run(
        {"required": lambda: _available("required")},
        environment="test",
        solidworks_revision="33.5.0",
        interop_version="33.5.0.53",
    )
    assert [item.state for item in statuses] == [
        CapabilityState.AVAILABLE,
        CapabilityState.UNKNOWN,
    ]
    assert lab.release_ready(environment="test") == (True, [])
    matrix = lab.latest_matrix(environment="test")
    assert {item["capability_id"] for item in matrix} == {"required", "optional"}


def test_product_family_worker_adapter_uses_staging_and_preserves_source(tmp_path):
    source = tmp_path / "source.SLDPRT"
    destination = tmp_path / "family.SLDPRT"
    source.write_bytes(b"source-part")
    variants = (
        FamilyVariant("Small", {"D1@Sketch1": 10.0}, {"PartNo": "A-10"}),
        FamilyVariant("Large", {"D1@Sketch1": 20.0}, {"PartNo": "A-20"}),
    )

    class Worker:
        def __init__(self):
            self.call_value: ToolCall | None = None

        def call(self, call, timeout_seconds=120):
            self.call_value = call
            staging = tmp_path / str(call.arguments["path"])
            assert staging.name.startswith(".family.sw-family-")
            staging.write_bytes(staging.read_bytes() + b"-configured")
            return StepResult(
                True,
                call,
                "ok",
                data={
                    "configurations": ["Small", "Large"],
                    "variants": call.arguments["variants"],
                },
                before=ModelSnapshot(),
                after=ModelSnapshot(),
            )

    worker = Worker()
    service = ProductFamilyService(WorkerProductFamilyAdapter(worker))
    output = service.generate(ProductFamilySpec(str(source), str(destination), variants))
    assert source.read_bytes() == b"source-part"
    assert destination.read_bytes() == b"source-part-configured"
    assert output["strategy"] == "configuration_copy"
    assert worker.call_value is not None
    assert worker.call_value.tool == "product_family.generate"
    assert not list(tmp_path.glob(".*.sw-family-*.SLDPRT"))


def test_product_family_gateway_rejects_inconsistent_dimension_columns():
    variants = [
        {"name": "A", "dimensions_mm": {"D1@Sketch1": 10}, "properties": {}},
        {"name": "B", "dimensions_mm": {"D2@Sketch1": 20}, "properties": {}},
    ]
    with pytest.raises(Exception, match="相同的尺寸列"):
        SolidWorksGateway._normalize_family_variants(variants)


def test_worker_advanced_adapters_map_structured_results(tmp_path):
    assembly_path = tmp_path / "fixture.SLDASM"
    assembly_path.write_bytes(b"assembly")

    class Worker:
        def __init__(self):
            self.calls = []

        def call(self, call, timeout_seconds=120):
            self.calls.append(call)
            if call.tool == "assembly.inspect":
                data = {
                    "report": {
                        "path": str(assembly_path),
                        "components": [
                            {
                                "name": "Base-1",
                                "path": "base.SLDPRT",
                                "fixed": True,
                                "mate_count": 3,
                            }
                        ],
                        "interferences": [
                            {
                                "component_a": "Base-1",
                                "component_b": "Cover-1",
                                "volume_m3": 1e-6,
                            }
                        ],
                    }
                }
            elif call.tool == "pmi.read":
                data = {
                    "pmi": [
                        {
                            "name": "孔径",
                            "kind": "diameter",
                            "value": 8,
                            "unit": "mm",
                        }
                    ]
                }
            else:
                data = {
                    "destination": call.arguments["destination"],
                    "views": [item["name"] for item in call.arguments["views"]],
                    "bom_created": False,
                }
            return StepResult(True, call, "ok", data=data)

    worker = Worker()

    def capability(identifier):
        return _available(identifier)

    assembly = WorkerAssemblyAdapter(worker, capability).inspect_assembly(
        str(assembly_path), cancel=threading.Event()
    )
    assert assembly.components[0].fixed is True
    assert assembly.interferences[0].volume_m3 == 1e-6

    model = tmp_path / "part.SLDPRT"
    template = tmp_path / "template.DRWDOT"
    destination = tmp_path / "part.SLDDRW"
    model.write_bytes(b"part")
    template.write_bytes(b"template")
    drawing_spec = DrawingSpec(
        str(model),
        str(destination),
        str(template),
        (DrawingViewSpec("主视图", "front"),),
    )
    drawing = WorkerDrawingAdapter(worker, capability).generate_drawing(
        drawing_spec, cancel=threading.Event()
    )
    assert drawing["views"] == ["主视图"]
    pmi = WorkerPmiAdapter(worker, capability).read_pmi(str(model), cancel=threading.Event())
    assert pmi[0].value == 8


def test_gateway_drawing_view_validation_supports_parented_projection():
    views = SolidWorksGateway._normalize_drawing_views(
        [
            {"name": "主视图", "orientation": "front", "scale": 1},
            {
                "name": "投影视图",
                "orientation": "projected",
                "projected_from": "主视图",
                "scale": 1,
            },
        ]
    )
    assert views[1]["projected_from"] == "主视图"
