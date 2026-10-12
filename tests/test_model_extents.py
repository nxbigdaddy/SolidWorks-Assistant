"""Bounded geometric extents using fake IDispatch and real in-memory VARIANTs."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from sw_assistant.domain import PlanningError, ToolCall
from sw_assistant.solidworks.gateway import SolidWorksGateway
from sw_assistant.tools import INVESTIGATION_TOOLS, build_tool_registry


class Entity:
    def __init__(self):
        self.identity = object()
        self._oleobj_ = SimpleNamespace(QueryInterface=lambda _iid: self.identity,
                                       GetIDsOfNames=lambda name: name, Invoke=self.invoke)

    def invoke(self, *_args):
        raise AssertionError("unexpected COM invocation")


class Body(Entity):
    def __init__(self, low=(-0.005, -0.005, 0.0), high=(0.005, 0.005, 0.01)):
        super().__init__()
        self.low, self.high, self.calls = low, high, []
        self.hook = None

    def invoke(self, name, lcid, flags, want_result, *args):
        import pythoncom
        from win32com.client import VARIANT

        assert name == "GetExtremePoint" and lcid == 0
        assert flags == pythoncom.DISPATCH_METHOD and want_result is True
        direction, outputs = args[:3], args[3:]
        assert len(outputs) == 3 and sum(abs(value) for value in direction) == 1
        assert all(isinstance(value, VARIANT) and value.varianttype == pythoncom.VT_BYREF | pythoncom.VT_R8
                   and math.isnan(value.value) for value in outputs)
        self.calls.append(direction)
        if self.hook:
            return self.hook(direction, outputs)
        for axis, value in enumerate(outputs):
            value.value = self.high[axis] if direction[axis] > 0 else self.low[axis]
        return True

    def __getattr__(self, name):
        raise AssertionError(f"Body must not use getter/fallback/mutation: {name}")


class Document(Entity):
    def __init__(self, bodies):
        super().__init__()
        self.bodies, self.body_reads = bodies, 0
        config = Entity()
        config.Name = "Default"
        self.ConfigurationManager = SimpleNamespace(ActiveConfiguration=config)
        self.title, self.path, self.code = "SyntheticCube", "", 1

    def GetTitle(self):
        return self.title

    def GetPathName(self):
        return self.path

    def GetType(self):
        return self.code

    def invoke(self, name, lcid, flags, want_result, body_type, visible_only):
        import pythoncom

        assert name == "GetBodies2" and lcid == 0 and flags == pythoncom.DISPATCH_METHOD and want_result is True
        assert body_type == 0 and visible_only is False
        self.body_reads += 1
        return self.bodies

    def __getattr__(self, name):
        raise AssertionError(f"Must not use approximate boxes, geometry traversal, or mutation: {name}")


@pytest.fixture
def setup(monkeypatch):
    import win32com.client

    monkeypatch.setattr(win32com.client, "Dispatch", lambda value: value)

    def create(bodies=None):
        gateway = SolidWorksGateway()
        document = Document([Body()] if bodies is None else bodies)
        gateway._sw = SimpleNamespace(ActiveDoc=document)
        return gateway, document

    return create


def measure(gateway):
    return gateway.execute(ToolCall("model.measure_extents", {}))[1]


def test_extents_are_opt_in_readonly_and_not_native_dimensions(setup):
    registry = build_tool_registry()
    assert "model.measure_extents" in INVESTIGATION_TOOLS
    assert registry.write_scope("model.measure_extents") == "none"
    assert registry.validate(ToolCall("model.measure_extents", {})).arguments == {}
    for field in ("path", "code", "axis", "limit"):
        with pytest.raises(PlanningError):
            registry.validate(ToolCall("model.measure_extents", {field: "unapproved"}))
    gateway, document = setup()
    result = measure(gateway)
    assert result["complete"] and result["issues"] == []
    assert result["extents_mm"] == {"x": 10.0, "y": 10.0, "z": 10.0}
    assert result["min_mm"] == [-5.0, -5.0, 0.0] and result["max_mm"] == [5.0, 5.0, 10.0]
    assert result["document_identity"] == gateway.probe_document_identity()["document_identity"]
    assert result["configuration"] == "Default" and result["configuration_identity"]
    assert result["source"] == "IBody2.GetExtremePoint" and result["coordinate_system"] == "model"
    assert result["bodies_count"] == 1 and len(document.bodies[0].calls) == 6
    assert "dimensions" not in result and document.body_reads == 2


def test_extents_union_all_solid_bodies_including_hidden_and_order_independent(setup):
    bodies = [Body(low=(0.1, 0.2, 0.3), high=(0.11, 0.22, 0.33)),
              Body(low=(-0.03, -0.02, -0.01), high=(-0.02, -0.01, 0))]
    gateway, document = setup(bodies)
    first = measure(gateway)
    document.bodies.reverse()
    assert measure(gateway) == first
    assert first["complete"] and first["bodies_count"] == 2
    assert first["extents_mm"] == pytest.approx({"x": 140, "y": 240, "z": 340})


def test_geometry_support_is_not_vertex_or_box_enumeration(setup):
    body = Body()

    def sphere_support(direction, outputs):
        for axis, output in enumerate(outputs):
            output.value = (0.013, -0.021, 0.03)[axis] + direction[axis] * 0.007
        return True

    body.hook = sphere_support
    gateway, _ = setup([body])
    assert measure(gateway)["extents_mm"] == pytest.approx({"x": 14, "y": 14, "z": 14})


@pytest.mark.parametrize("mode", ["false", "unset", "partial", "nan", "inf", "exception", "inverted", "zero"])
def test_unreadable_extreme_point_never_returns_partial_or_zero_extents(setup, mode):
    first, body = Body(), Body()

    def failed(direction, outputs):
        if mode == "exception":
            raise RuntimeError("synthetic COM failure")
        if mode == "false":
            return False
        if mode == "unset":
            return True
        for axis, output in enumerate(outputs):
            if mode == "partial" and axis == 2:
                continue
            output.value = (float("nan") if mode == "nan" else float("inf") if mode == "inf"
                            else -direction[axis] if mode == "inverted" else 0.0)
        return True

    body.hook = failed
    gateway, _ = setup([body] if mode == "zero" else [first, body])
    result = measure(gateway)
    assert not result["complete"] and result["extents_mm"] == {"x": None, "y": None, "z": None}
    assert "min_mm" not in result and result["issues"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("mode", ["disconnected", "no_doc", "assembly", "empty", "too_many", "duplicate",
                                  "bad_collection", "bad_config", "unknown_identity"])
def test_extents_require_known_bounded_part_and_configuration(setup, mode):
    gateway, document = setup()
    if mode == "disconnected":
        gateway._sw = None
    elif mode == "no_doc":
        gateway._sw.ActiveDoc = None
    elif mode == "assembly":
        document.code = 2
    elif mode == "empty":
        document.bodies = []
    elif mode == "too_many":
        document.bodies = [Body() for _ in range(65)]
    elif mode == "duplicate":
        document.bodies *= 2
    elif mode == "bad_collection":
        document.bodies = {"not": "a COM array"}
    elif mode == "bad_config":
        document.ConfigurationManager.ActiveConfiguration.Name = None
    else:
        document._oleobj_.QueryInterface = lambda _: None
    result = measure(gateway)
    assert not result["complete"] and all(value is None for value in result["extents_mm"].values())
    assert result["issues"]


@pytest.mark.parametrize("mode", ["doc", "title", "path", "config_name", "config_identity", "body_identity"])
def test_extents_reject_identity_or_configuration_drift_after_measurement(setup, mode):
    gateway, document = setup()
    body = document.bodies[0]
    original = body._oleobj_.Invoke

    def invoke(*args):
        result = original(*args)
        if len(body.calls) == 6:
            if mode == "doc":
                gateway._sw.ActiveDoc = Document([Body()])
            elif mode == "title":
                document.title = "changed"
            elif mode == "path":
                document.path = "C:/synthetic/saved.sldprt"
            elif mode == "config_name":
                document.ConfigurationManager.ActiveConfiguration.Name = "Other"
            elif mode == "config_identity":
                document.ConfigurationManager.ActiveConfiguration.identity = object()
            else:
                document.bodies = [Body()]
        return result

    body._oleobj_.Invoke = invoke
    result = measure(gateway)
    assert not result["complete"] and result["issues"]
    assert all(value is None for value in result["extents_mm"].values())
