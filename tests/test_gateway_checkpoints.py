from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sw_assistant.solidworks.checkpoints import (
    CandidateStateChanged, CandidateWorkspaceMixin, _CANDIDATE_COM_SIGNATURES, _equivalent, _state,
)


class Document:
    def __init__(self, identity="source", path="", *, volume=1e-6):
        self.identity = identity
        self.path = path
        self.title = Path(path).name if path else "Part1"
        self.stamp = 1
        self.volume = volume
        self.selection = 0
        self.active_sketch = ""
        self.feature_error = 0
        self.complete = True
        self.geometry_complete = True
        self.config_count = 1
        self.external_refs = 0
        self.auxiliary_refs = 0
        self.open_error = 0
        self.save_result = (True, 0, 0)
        self.saved_options = []
        self.config = SimpleNamespace(Name="Default", identity=f"{identity}-config")
        self.ConfigurationManager = SimpleNamespace(ActiveConfiguration=self.config)
        self.Extension = SimpleNamespace(
            ListExternalFileReferencesCount=lambda: self.external_refs,
            SaveAs3=self.save_as,
        )
        self.on_save = None
        self.first_feature = None

    def GetType(self):
        return 1

    def GetTitle(self):
        return self.title

    def GetPathName(self):
        return self.path

    def GetUpdateStamp(self):
        return self.stamp

    def GetConfigurationCount(self):
        return self.config_count

    def ListAuxiliaryExternalFileReferencesCount(self):
        return self.auxiliary_refs

    def FirstFeature(self):
        if self.first_feature is not None:
            return self.first_feature
        boss = NativeFeature("Boss1", "Boss") if self.volume else None
        return NativeFeature("Front Plane", "RefPlane", next_feature=boss)

    def GetBodies2(self, *_):
        return [] if not self.volume else [object()]

    def save_as(self, path, version, options, *_):
        self.saved_options.append((path, version, options))
        Path(path).write_bytes(b"synthetic native part fixture")
        if self.on_save:
            self.on_save()
        return self.save_result


class NativeFeature:
    def __init__(self, name, kind, *, next_feature=None):
        self.Name, self.kind, self.next_feature = name, kind, next_feature
        self.identity = name

    def GetTypeName2(self):
        return self.kind

    def GetNextFeature(self):
        return self.next_feature

    def GetFirstSubFeature(self):
        return None


class Application:
    def __init__(self, document):
        self.ActiveDoc = document
        self.documents = [document]
        self.activations = []
        self.on_open = None
        self.on_activate = None
        self.open_codes = (0, 0)

    def OpenDoc6(self, path, kind, options, config, *_):
        source = self.ActiveDoc
        assert kind == 1 and options == 1 and config == "Default"
        candidate = Document(Path(path).stem, path, volume=source.volume)
        candidate.first_feature = source.first_feature
        self.ActiveDoc = candidate
        self.documents.append(candidate)
        if self.on_open:
            self.on_open(candidate)
        return candidate, *self.open_codes

    def ActivateDoc3(self, name, preferences, rebuild, *_):
        self.activations.append((name, preferences, rebuild))
        assert preferences is False and rebuild == 1
        document = next(item for item in self.documents if name in {item.title, item.path})
        self.ActiveDoc = document
        if self.on_activate:
            self.on_activate(document)
        return document, 0

    def EditUndo2(self, *_):
        raise AssertionError("no Undo operation is permitted")

    def CloseDoc(self, *_):
        raise AssertionError("no document may be closed")


class Gateway(CandidateWorkspaceMixin):
    def __init__(self, document):
        self._sw = Application(document)
        self._owned_circles = {"baseline": {"value": 1}}
        self._last_terminal_error = "original context"
        self.observe_hook = None

    def _com_identity(self, obj):
        return obj.identity

    def require_document(self, *, part=False):
        return self._sw.ActiveDoc

    def probe_document_identity(self):
        document = self._sw.ActiveDoc
        return {"document_identity": document.identity}

    def _observe_model(self, include_configuration=False):
        assert include_configuration is True
        document = self._sw.ActiveDoc
        if self.observe_hook:
            self.observe_hook(document)
        features = []
        feature = document.FirstFeature()
        seen = set()
        while feature is not None and feature.identity not in seen:
            seen.add(feature.identity)
            features.append({"name": feature.Name, "type": feature.GetTypeName2(),
                             "error_code": document.feature_error})
            feature = feature.GetNextFeature()
        return {
            "snapshot": {
                "connected": True, "document_type": "part", "document_path": document.path,
                "document_title": document.title, "body_count": 1 if document.volume else 0,
                "feature_count": len(features), "selection_count": document.selection,
                "active_sketch": document.active_sketch, "sketch_entity_count": 0,
                "volume_m3": document.volume, "surface_area_m2": 0.0006,
                "captured_at": str(id(object())),
            },
            "complete": document.complete, "issues": [] if document.complete else ["read failed"],
            "features": features, "dimensions": [{"name": "D1@Boss1", "system_value": 0.01}],
            "planes": [{"name": "Front Plane"}],
            "selection": [{"type": 2, "name": "Face1"}] * document.selection,
            "document_identity": document.identity, "configuration": document.config.Name,
            "configuration_identity": document.config.identity,
            "active_sketch_geometry_complete": document.geometry_complete,
            "active_sketch_geometry": [] if not document.active_sketch else {"segments": []},
        }

    def _measure_extents(self):
        document = self._sw.ActiveDoc
        return {"complete": bool(document.volume), "issues": [] if document.volume else ["empty"],
                "document_identity": document.identity, "document_path": document.path,
                "configuration_identity": document.config.identity,
                "extents_mm": {"x": 10.0, "y": 10.0, "z": 10.0}}

    def _reset_modeling_context(self, reason=""):
        self._owned_circles.clear()
        self._last_terminal_error = reason

    @staticmethod
    def _int_reference():
        return SimpleNamespace(value=0)

    @staticmethod
    def _null_dispatch():
        return None

    @staticmethod
    def _segment_geometry(segment):
        return copy.deepcopy(segment)


@pytest.fixture
def workspace(tmp_path):
    source = Document()
    gateway = Gateway(source)
    gateway.configure_candidate_workspace(tmp_path / "candidates")
    gateway.register_candidate_document(source)
    return gateway, source


def observation(gateway):
    return gateway._observe_model(include_configuration=True)


def begin(gateway, task="task-1"):
    return gateway.begin_candidate(observation(gateway), task)


def test_rejected_candidate_preserves_original_and_native_context(workspace):
    gateway, source = workspace
    result = begin(gateway)
    assert result["ok"], result
    candidate = gateway._sw.ActiveDoc
    assert candidate is not source
    assert gateway._owned_circles == {}
    candidate.volume = 2e-6
    candidate.stamp += 1
    candidate.active_sketch = "FailedSketch"
    gateway._owned_circles["candidate"] = {"value": 2}
    rejected = gateway.finish_candidate(result["token"], False, observation(gateway))
    assert rejected["ok"] and rejected["status"] == "restored", rejected
    assert rejected["native_context_restored"] is True
    assert gateway._sw.ActiveDoc is source
    assert source.volume == 1e-6 and source.stamp == 1
    assert gateway._owned_circles == {"baseline": {"value": 1}}
    assert gateway._last_terminal_error == "original context"
    assert candidate in gateway._sw.documents
    assert Path(result["artifact_path"]).is_file()
    assert source.saved_options[0][1:] == (0, 11)
    assert len(gateway._sw.documents) == 2


def test_committed_copy_can_become_next_baseline_only_for_same_task(workspace):
    gateway, source = workspace
    first = begin(gateway)
    candidate = gateway._sw.ActiveDoc
    candidate.volume = 2e-6
    candidate.stamp += 1
    committed = gateway.finish_candidate(first["token"], True, observation(gateway))
    assert committed["status"] == "committed" and committed["ok"]
    assert committed["persisted_current_state"] is True
    assert committed["checkpoint_id"]
    assert Path(committed["checkpoint_path"]).is_file()
    assert gateway._sw.ActiveDoc is candidate
    denied = begin(gateway, "different-task")
    assert not denied["ok"] and "different task" in denied["reason"]
    second = begin(gateway)
    assert second["ok"], second
    assert second["baseline"]["document_identity"] == candidate.identity
    assert len(gateway._sw.documents) == 3
    assert source.volume == 1e-6


@pytest.mark.skipif(os.name != "nt", reason="native SOLIDWORKS path spelling follows Windows semantics")
def test_native_uppercase_extension_remains_owned_for_next_candidate(workspace):
    gateway, _ = workspace

    def normalize_native_path(document):
        document.path = str(Path(document.path).with_suffix(".SLDPRT"))
        document.title = Path(document.path).name

    gateway._sw.on_open = normalize_native_path
    first = begin(gateway)
    assert first["ok"], first
    candidate = gateway._sw.ActiveDoc
    assert candidate.path.endswith(".SLDPRT")
    accepted = gateway.finish_candidate(first["token"], True, observation(gateway))
    assert accepted["ok"], accepted
    assert gateway._candidate_owned[candidate.identity].path == candidate.path
    second = begin(gateway)
    assert second["ok"], second
    assert second["baseline"]["document_identity"] == candidate.identity


@pytest.mark.parametrize("field,value", [
    ("selection", 1), ("active_sketch", "Sketch1"), ("complete", False),
    ("geometry_complete", False), ("external_refs", 1), ("auxiliary_refs", 1),
    ("config_count", 2), ("feature_error", 1),
])
def test_unsupported_or_unclean_source_never_saves(workspace, field, value):
    gateway, source = workspace
    setattr(source, field, value)
    result = begin(gateway)
    assert not result["ok"]
    assert source.saved_options == []
    assert len(gateway._sw.documents) == 1


def test_unregistered_and_saved_user_documents_cannot_gain_authority(tmp_path):
    source = Document(path=str(tmp_path / "user.sldprt"))
    gateway = Gateway(source)
    gateway.configure_candidate_workspace(tmp_path / "candidates")
    with pytest.raises(ValueError, match="unsaved"):
        gateway.register_candidate_document(source)
    assert not begin(gateway)["ok"]
    source.path = ""
    assert not begin(gateway)["ok"]
    assert not source.saved_options


def test_stale_baseline_and_task_path_injection_cannot_write_outside_root(workspace):
    gateway, source = workspace
    expected = observation(gateway)
    source.volume *= 2
    result = gateway.begin_candidate(expected, "task")
    assert not result["ok"] and not source.saved_options
    result = begin(gateway, "../../elsewhere")
    assert result["ok"], result
    assert Path(result["artifact_path"]).is_relative_to(gateway._candidate_root)
    assert "elsewhere" not in result["artifact_path"]


@pytest.mark.parametrize("change", ["candidate", "source", "configuration", "path"])
def test_finish_refuses_external_change_without_switching(workspace, change):
    gateway, source = workspace
    result = begin(gateway)
    candidate = gateway._sw.ActiveDoc
    expected = observation(gateway)
    if change == "candidate":
        candidate.volume += 1e-6
    elif change == "source":
        source.stamp += 1
    elif change == "configuration":
        candidate.config.Name = "Other"
    else:
        candidate.path = str(Path(candidate.path).with_name("user-renamed.sldprt"))
    count = len(gateway._sw.activations)
    finished = gateway.finish_candidate(result["token"], False, expected)
    assert not finished["ok"] and finished["status"] == "unavailable"
    assert gateway._sw.ActiveDoc is candidate
    assert len(gateway._sw.activations) == count


def test_user_switch_and_worker_restart_do_not_switch_or_restore(workspace):
    gateway, source = workspace
    result = begin(gateway)
    expected = observation(gateway)
    external = Document("external")
    gateway._sw.documents.append(external)
    gateway._sw.ActiveDoc = external
    finished = gateway.finish_candidate(result["token"], False, expected)
    assert not finished["ok"] and gateway._sw.ActiveDoc is external
    gateway.invalidate_candidate_workspace()
    gateway._sw.ActiveDoc = source
    assert not gateway.finish_candidate(result["token"], False, expected)["ok"]
    assert not begin(gateway)["ok"]


@pytest.mark.parametrize("error", ["save_warning", "open_warning", "copy_geometry", "save_mutation"])
def test_begin_failure_preserves_artifacts_and_only_claims_verified_recovery(workspace, error):
    gateway, source = workspace
    if error == "save_warning":
        source.save_result = (True, 0, 1)
    elif error == "open_warning":
        gateway._sw.open_codes = (0, 1)
    elif error == "copy_geometry":
        gateway._sw.on_open = lambda doc: setattr(doc, "volume", 9e-6)
    else:
        source.on_save = lambda: setattr(source, "stamp", 2)
    result = begin(gateway)
    assert not result["ok"], result
    assert Path(result["artifact_path"]).is_file()
    if error == "save_mutation":
        assert result.get("baseline_unchanged") is not True
        assert result["recovery"]["ok"] is False
    else:
        assert result["baseline_unchanged"] is True, result
        assert result["observation"]["document_identity"] == source.identity
        assert gateway._sw.ActiveDoc is source


def test_no_operation_rejection_and_nested_trial_are_safe(workspace):
    gateway, source = workspace
    result = begin(gateway)
    assert not begin(gateway)["ok"]
    restored = gateway.finish_candidate(result["token"], False, observation(gateway))
    assert restored["ok"] and restored["status"] == "restored"
    assert gateway._sw.ActiveDoc is source
    assert not gateway.finish_candidate(result["token"], False, observation(gateway))["ok"]


def test_cancelled_host_can_leave_candidate_without_any_cleanup_action(workspace):
    gateway, source = workspace
    result = begin(gateway)
    assert result["ok"]
    candidate = gateway._sw.ActiveDoc
    # Cancellation means the host dispatches no finish RPC.  Invalidating a
    # killed worker only forgets authority; no destructor restores/closes CAD.
    gateway.invalidate_candidate_workspace()
    assert gateway._sw.ActiveDoc is candidate
    assert source in gateway._sw.documents
    assert len(gateway._sw.activations) == 0


def test_source_change_during_restore_is_not_reported_as_restored(workspace):
    gateway, source = workspace
    result = begin(gateway)
    gateway._sw.on_activate = lambda doc: setattr(doc, "stamp", 99)
    finished = gateway.finish_candidate(result["token"], False, observation(gateway))
    assert not finished["ok"] and finished["status"] == "unavailable"
    assert source.stamp == 99


def test_empty_new_part_is_valid_baseline(workspace):
    gateway, source = workspace
    source.volume = 0
    result = begin(gateway)
    assert result["ok"], result


def test_read_changes_mid_observation_are_rejected(workspace):
    gateway, source = workspace
    expected = observation(gateway)
    gateway.observe_hook = lambda doc: setattr(doc, "stamp", doc.stamp + 1)
    result = gateway.begin_candidate(expected, "task")
    assert not result["ok"] and not source.saved_options


def test_evidence_rejects_nan_and_boolean_counts(workspace):
    gateway, _ = workspace
    value = observation(gateway)
    value["snapshot"]["body_count"] = True
    with pytest.raises(ValueError):
        _state(value)
    value = observation(gateway)
    value["dimensions"][0]["system_value"] = float("nan")
    with pytest.raises(ValueError):
        _state(value)
    assert not _equivalent(True, 1)
    assert not _equivalent(float("nan"), float("nan"))


def test_workspace_is_absolute_and_immutable(tmp_path):
    gateway = Gateway(Document())
    with pytest.raises(ValueError, match="absolute"):
        gateway.configure_candidate_workspace("relative")
    gateway.configure_candidate_workspace(tmp_path / "one")
    with pytest.raises(ValueError, match="cannot change"):
        gateway.configure_candidate_workspace(tmp_path / "two")


class SketchFeature:
    def __init__(self, name="Sketch1", *, sub=None, next_feature=None):
        self.Name = name
        self.identity = name
        self.sub = sub
        self.next_feature = next_feature
        self.next_sub = None
        self.sketch = SimpleNamespace(
            Is3D=False,
            GetSketchSegments=lambda: [{"type": 0, "construction": False,
                                       "start_sketch_m": [0.0, 0.0, 0.0],
                                       "end_sketch_m": [0.01, 0.0, 0.0]}],
            ModelToSketchTransform=SimpleNamespace(ArrayData=[1.0] * 16),
        )

    def GetTypeName2(self):
        return "ProfileFeature"

    def GetSpecificFeature2(self):
        return self.sketch

    def GetFirstSubFeature(self):
        return self.sub

    def GetNextFeature(self):
        return self.next_feature

    def GetNextSubFeature(self):
        return self.next_sub


def test_all_sketch_and_absorbed_subfeature_geometry_is_read(workspace):
    gateway, source = workspace
    absorbed = SketchFeature("AbsorbedSketch")
    source.first_feature = SketchFeature(sub=absorbed, next_feature=absorbed)
    evidence = gateway._candidate_geometry(source)
    assert [item["name"] for item in evidence["sketches"]] == ["AbsorbedSketch", "Sketch1"]
    assert len(evidence["sketches"][0]["segments"]) == 1
    assert begin(gateway)["ok"]


@pytest.mark.parametrize("failure", ["cycle", "3d", "transform", "unsupported", "nan"])
def test_unreadable_sketch_fidelity_refuses_native_copy(workspace, failure):
    gateway, source = workspace
    feature = SketchFeature()
    source.first_feature = feature
    if failure == "cycle":
        feature.next_feature = feature
    elif failure == "3d":
        feature.sketch.Is3D = True
    elif failure == "transform":
        feature.sketch.ModelToSketchTransform.ArrayData = [1, 2]
    elif failure == "nan":
        feature.sketch.ModelToSketchTransform.ArrayData[0] = float("nan")
    else:
        def unsupported(_):
            raise ValueError("unsupported sketch segment")
        gateway._segment_geometry = unsupported
    assert not begin(gateway)["ok"]
    assert source.saved_options == []


def test_document_switch_while_saving_does_not_activate_an_external_document(workspace):
    gateway, source = workspace
    external = Document("external")
    source.on_save = lambda: setattr(gateway._sw, "ActiveDoc", external)
    result = begin(gateway)
    assert not result["ok"] and result.get("baseline_unchanged") is not True
    assert gateway._sw.ActiveDoc is external
    assert gateway._sw.activations == []


def test_candidate_destination_collision_is_not_overwritten(workspace, tmp_path):
    gateway, source = workspace
    path = gateway._candidate_root / "existing.sldprt"
    path.write_bytes(b"user-owned bytes")
    with pytest.raises(ValueError, match="no longer a new"):
        gateway._candidate_save_copy(source, path)
    assert path.read_bytes() == b"user-owned bytes"
    assert source.saved_options == []


def test_com_generated_and_dynamic_result_layouts_are_strict(workspace):
    gateway, source = workspace
    zero = SimpleNamespace(value=0)
    assert gateway._candidate_com_result((source, 0, 0), zero, zero) == (source, [0, 0])
    assert gateway._candidate_com_result(source, zero) == (source, [0])
    with pytest.raises(ValueError):
        gateway._candidate_com_result((source, 0), zero, zero)
    with pytest.raises(ValueError):
        gateway._candidate_com_result(source, object())


def test_false_commit_value_cannot_be_coerced_from_string(workspace):
    gateway, _ = workspace
    trial = begin(gateway)
    result = gateway.finish_candidate(trial["token"], "false", observation(gateway))
    assert not result["ok"] and result["status"] == "unavailable"


def test_source_feature_rename_without_stamp_change_refuses_switch(workspace):
    gateway, source = workspace
    source.first_feature = NativeFeature("Boss1", "Boss")
    trial = begin(gateway)
    assert trial["ok"], trial
    source.first_feature.Name = "User Renamed"
    before = len(gateway._sw.activations)
    result = gateway.finish_candidate(trial["token"], False, observation(gateway))
    assert not result["ok"] and "feature tree changed" in result["reason"]
    assert len(gateway._sw.activations) == before


def test_update_stamp_is_required_when_host_observation_provides_it(workspace):
    gateway, source = workspace
    observe_original = gateway._observe_model

    def stamped(include_configuration=False):
        return {**observe_original(include_configuration=include_configuration),
                "model_update_stamp": gateway._sw.ActiveDoc.stamp}

    gateway._observe_model = stamped
    trial = begin(gateway)
    assert trial["ok"], trial
    expected = observation(gateway)
    candidate = gateway._sw.ActiveDoc
    candidate.stamp += 1  # Same volume/features/extents does not prove same state.
    result = gateway.finish_candidate(trial["token"], True, expected)
    assert not result["ok"] and result["status"] == "unavailable"
    assert gateway._sw.ActiveDoc is candidate
    assert source.stamp == 1


def test_copy_fidelity_ignores_distinct_native_document_update_stamps(workspace):
    gateway, _ = workspace
    observe_original = gateway._observe_model

    def stamped(include_configuration=False):
        return {**observe_original(include_configuration=include_configuration),
                "model_update_stamp": gateway._sw.ActiveDoc.stamp}

    gateway._observe_model = stamped
    gateway._sw.on_open = lambda document: setattr(document, "stamp", 7)
    trial = begin(gateway)
    assert trial["ok"], trial
    assert trial["baseline"]["model_update_stamp"] == 1
    assert trial["candidate"]["model_update_stamp"] == 7


@pytest.mark.parametrize("changed", ["active", "config", "source", "stamp", "helper"])
def test_candidate_write_guard_rejects_foreign_or_changed_native_target(workspace, changed):
    gateway, source = workspace
    assert begin(gateway)["ok"]
    candidate = gateway._sw.ActiveDoc
    foreign = Document("foreign")
    target = candidate
    if changed == "active":
        gateway._sw.ActiveDoc = foreign
    elif changed == "config":
        candidate.config.Name = "User Config"
    elif changed == "source":
        source.stamp += 1
    elif changed == "stamp":
        candidate.stamp += 1
    else:
        target = foreign
    with pytest.raises(CandidateStateChanged):
        gateway.candidate_write_guard(target, check_stamp=True)
    assert not gateway._sw.activations


def test_internal_write_guard_allows_own_stamp_progress_but_next_write_needs_observation(workspace):
    gateway, _ = workspace
    assert begin(gateway)["ok"]
    candidate = gateway._sw.ActiveDoc
    gateway.candidate_write_guard(candidate, check_stamp=True)
    candidate.stamp += 1
    gateway.candidate_write_guard(candidate, check_stamp=False)
    with pytest.raises(CandidateStateChanged, match="last complete"):
        gateway.candidate_write_guard(candidate, check_stamp=True)
    gateway._candidate_observe()
    gateway.candidate_write_guard(candidate, check_stamp=True)


def test_guard_no_trial_does_not_create_or_adopt_ownership(workspace):
    gateway, source = workspace
    gateway.invalidate_candidate_workspace()
    gateway.candidate_write_guard(source, check_stamp=True)
    assert gateway._candidate_owned == {}


class TypedOle:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.lookups = []

    def GetIDsOfNames(self, name):
        self.lookups.append(name)
        return {"SaveAs3": 315, "OpenDoc6": 167, "ActivateDoc3": 306}[name]

    def InvokeTypes(self, *arguments):
        self.calls.append(arguments)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class EarlyBoundWrapper:
    def __init__(self, ole):
        self._oleobj_ = ole
        self.wrapper_calls = []

    def SaveAs3(self, *args):
        self.wrapper_calls.append("SaveAs3")
        raise AssertionError("generated wrapper must be bypassed")

    def OpenDoc6(self, *args):
        self.wrapper_calls.append("OpenDoc6")
        raise AssertionError("generated wrapper must be bypassed")

    def ActivateDoc3(self, *args):
        self.wrapper_calls.append("ActivateDoc3")
        raise AssertionError("generated wrapper must be bypassed")


class LateBoundWrapper:
    def __init__(self, ole):
        self._oleobj_ = ole

    def __getattr__(self, name):
        raise AssertionError(f"late-bound eager lookup must be bypassed: {name}")


@pytest.mark.parametrize("wrapper", [EarlyBoundWrapper, LateBoundWrapper])
@pytest.mark.parametrize("name,inputs", [
    ("SaveAs3", ("C:/owned/candidate.sldprt", 0, 11, None, None)),
    ("OpenDoc6", ("C:/owned/candidate.sldprt", 1, 1, "Default")),
    ("ActivateDoc3", ("C:/owned/candidate.sldprt", False, 1)),
])
def test_native_typed_invocation_bypasses_binding_style_once(workspace, monkeypatch, wrapper, name, inputs):
    import win32com.client

    gateway, source = workspace
    result_type, argument_types, input_count, output_count = _CANDIDATE_COM_SIGNATURES[name]
    raw_dispatch = object()
    raw_value = True if name == "SaveAs3" else raw_dispatch
    ole = TypedOle((raw_value, *([0] * output_count)))
    wrapped = wrapper(ole)
    dispatch_calls = []

    def dispatch(value):
        dispatch_calls.append(value)
        assert value is raw_dispatch
        return source

    monkeypatch.setattr(win32com.client, "Dispatch", dispatch)
    value, codes = gateway._candidate_invoke(wrapped, name, *inputs)
    assert value is (True if name == "SaveAs3" else source)
    assert codes == [0] * output_count
    assert ole.lookups == [name]
    assert len(ole.calls) == 1
    call = ole.calls[0]
    expected_id = {"SaveAs3": 315, "OpenDoc6": 167, "ActivateDoc3": 306}[name]
    assert call[:5] == (expected_id, 0, 1, result_type, argument_types)
    assert call[5:] == (*inputs, *([0] * output_count))
    assert len(inputs) == input_count
    assert all(isinstance(item, int) for item in call[-output_count:])
    assert dispatch_calls == ([] if name == "SaveAs3" else [raw_dispatch])
    if isinstance(wrapped, EarlyBoundWrapper):
        assert wrapped.wrapper_calls == []


@pytest.mark.parametrize("result", [RuntimeError("COM call may already have run"),
                                    True, (True,), (True, 0), (True, 0, None), (True, 0, False)])
def test_typed_save_failure_or_missing_outputs_never_retries(workspace, result):
    gateway, _ = workspace
    ole = TypedOle(result)
    wrapped = EarlyBoundWrapper(ole)
    with pytest.raises((RuntimeError, ValueError)):
        gateway._candidate_invoke(wrapped, "SaveAs3", "C:/owned/test.sldprt", 0, 11, None, None)
    assert len(ole.calls) == 1
    assert wrapped.wrapper_calls == []


def test_typed_candidate_methods_are_fixed_allowlist_and_argument_count(workspace):
    gateway, _ = workspace
    ole = TypedOle((True, 0, 0))
    wrapped = EarlyBoundWrapper(ole)
    with pytest.raises(ValueError, match="allowlisted"):
        gateway._candidate_invoke(wrapped, "EditUndo2", 1)
    with pytest.raises(ValueError, match="argument count"):
        gateway._candidate_invoke(wrapped, "SaveAs3", "C:/owned/test.sldprt")
    assert not ole.calls and not ole.lookups
