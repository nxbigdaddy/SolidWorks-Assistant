"""Host-owned native-copy experiments; never infer recovery from an Undo count.

An experiment edits a new native copy, not the verified source document.  Rejecting
it only activates the still-open source and independently checks its evidence.
The host must stop dispatching these RPCs when cancellation is requested.  No
method here closes a document, deletes a file, executes Undo, or accepts a path
from the model.  Ownership and tokens are intentionally worker-local.

Official API contracts used here (SOLIDWORKS 2025):
* IModelDocExtension.SaveAs3 / swSaveAsOptions_e: Silent=1, Copy=2,
  AvoidRebuildOnSave=8.  SaveReferenced and CopyAndOpen are deliberately absent.
* ISldWorks.ActivateDoc3 / swRebuildOnActivation_e: DontRebuildActiveDoc=1.
* IModelDocExtension.ListExternalFileReferencesCount: copies with references
  require a different workflow and are rejected, not silently detached.

This is bounded evidence for native-copy fidelity, not a general B-rep theorem.
Unsupported/incomplete sketch or configuration evidence disables the experiment.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..domain import ToolError
from .checkpoint_store import CheckpointStore, validate_checkpoint, validate_native_path, validate_task


class CandidateStateChanged(ToolError):
    """A trial write lost its native target; do not automatically switch back."""


_CONTEXT_FIELDS = (
    "_last_centerline", "_pending_revolve", "_sketch_session", "_sketch_frame",
    "_last_terminal_signature", "_last_terminal_frame", "_last_terminal_error",
    "_owned_circles",
)

# Fixed SOLIDWORKS type-library signatures.  VT_I4|VT_BYREF is an IN/OUT
# LONG (flags=3); InvokeTypes takes a plain initial integer and returns its
# output in the result tuple.  Passing a win32com VARIANT to an early-bound
# generated wrapper instead fails int(VARIANT), while retrying a mutating
# operation through another wrapper could execute it twice.
_CANDIDATE_COM_SIGNATURES = {
    "SaveAs3": ((11, 0), ((8, 1), (3, 1), (3, 1), (9, 1), (9, 1),
                           (16387, 3), (16387, 3)), 5, 2),
    "OpenDoc6": ((9, 0), ((8, 1), (3, 1), (3, 1), (8, 1),
                           (16387, 3), (16387, 3)), 4, 2),
    "ActivateDoc3": ((9, 0), ((8, 1), (11, 1), (3, 1), (16387, 3)), 3, 1),
}


def _read(obj: Any, member: str, *args: Any) -> Any:
    value = getattr(obj, member)
    if hasattr(value, "_oleobj_"):
        return value
    return value(*args) if callable(value) else value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} is unavailable")
    return value


def _equivalent(left: Any, right: Any) -> bool:
    """Native save/reload may round floats, but never permit count/type drift."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (float, int)) and isinstance(right, (float, int)):
        return math.isfinite(left) and math.isfinite(right) and math.isclose(
            left, right, rel_tol=1e-9, abs_tol=1e-12
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equivalent(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _equivalent(a, b) for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def _state(observation: dict[str, Any], *, identity: bool = True) -> dict[str, Any]:
    """Keep the strict observation contract, excluding only wall-clock fields."""
    if observation.get("complete") is not True or observation.get("issues") != []:
        raise ValueError("complete observation is required")
    snapshot = observation.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot is missing")
    if snapshot.get("connected") is not True or snapshot.get("document_type") != "part":
        raise ValueError("a connected part is required")
    for name in ("document_identity", "configuration", "configuration_identity"):
        if not isinstance(observation.get(name), str) or not observation[name]:
            raise ValueError(f"{name} is missing")
    for name in ("body_count", "feature_count", "selection_count", "sketch_entity_count"):
        _integer(snapshot.get(name), name)
    for name in ("volume_m3", "surface_area_m2"):
        number = snapshot.get(name)
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or number < 0):
            raise ValueError(f"{name} is missing")
    if not isinstance(snapshot.get("active_sketch"), str):
        raise ValueError("active sketch is unknown")
    if observation.get("active_sketch_geometry_complete") is not True:
        raise ValueError("complete active sketch geometry is required")
    if not isinstance(observation.get("active_sketch_geometry"), (dict, list)):
        raise ValueError("active sketch geometry is missing")
    result: dict[str, Any] = {}
    for name in ("features", "dimensions", "planes", "selection"):
        if not isinstance(observation.get(name), list):
            raise ValueError(f"{name} is missing")
        result[name] = copy.deepcopy(observation[name])
    if len(result["features"]) != snapshot["feature_count"]:
        raise ValueError("feature evidence count differs")
    if len(result["selection"]) != snapshot["selection_count"]:
        raise ValueError("selection evidence count differs")
    result.update(
        snapshot={key: value for key, value in snapshot.items() if key != "captured_at"},
        configuration=observation["configuration"],
        active_sketch_geometry=copy.deepcopy(observation.get("active_sketch_geometry")),
    )
    if identity:
        result["document_identity"] = observation["document_identity"]
        result["configuration_identity"] = observation["configuration_identity"]
        if "model_update_stamp" in observation:
            result["model_update_stamp"] = _integer(
                observation["model_update_stamp"], "model update stamp"
            )
    else:
        for name in ("document_title", "document_path"):
            result["snapshot"].pop(name, None)
    # Reject NaN/Infinity anywhere, including dimensions or geometry arrays.
    json.dumps(result, allow_nan=False)
    return result


def _clean(observation: dict[str, Any]) -> None:
    state = _state(observation)
    snapshot = state["snapshot"]
    if snapshot["active_sketch"] or snapshot["selection_count"]:
        raise ValueError("candidate boundary requires no active sketch and no selection")
    if any(item.get("error_code") != 0 for item in state["features"]):
        raise ValueError("candidate baseline contains feature errors")


@dataclass
class _OwnedDocument:
    document: Any
    path: str
    task_id: str | None = None
    checkpoint_id: str | None = None
    observation: dict[str, Any] | None = None
    geometry: dict[str, Any] | None = None
    header: dict[str, Any] | None = None
    stamp: int | None = None


@dataclass
class _Candidate:
    token: str
    task_id: str
    source: Any
    source_header: dict[str, Any]
    source_stamp: int
    source_observation: dict[str, Any]
    source_geometry: dict[str, Any]
    source_context: dict[str, Any]
    path: Path
    document: Any = None
    header: dict[str, Any] | None = None
    last_observed_stamp: int | None = None


class CandidateWorkspaceMixin:
    """Mixin for SolidWorksGateway; these methods are host RPCs, not CAD tools."""

    def configure_candidate_workspace(self, root: Path | str) -> None:
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("candidate workspace must be an absolute host path")
        path = path.resolve()
        previous = getattr(self, "_candidate_root", None)
        if previous is not None and previous != path:
            raise ValueError("candidate workspace cannot change within a worker")
        path.mkdir(parents=True, exist_ok=True)
        self._candidate_root = path
        if not hasattr(self, "_candidate_owned"):
            self._candidate_owned: dict[str, _OwnedDocument] = {}
            self._candidate_active: _Candidate | None = None

    def register_candidate_document(self, document: Any) -> None:
        """Only call immediately after *proven* creation by document.new_part."""
        gateway = cast(Any, self)
        if not hasattr(self, "_candidate_owned"):
            self._candidate_owned = {}
            self._candidate_active = None
        if self._candidate_active is not None:
            raise ValueError("cannot create a new source inside a candidate")
        header = self._candidate_header(document)
        if header["document_path"] or header["document_type"] != "part":
            raise ValueError("only a newly created unsaved part can be registered")
        if gateway.probe_document_identity()["document_identity"] != header["document_identity"]:
            raise ValueError("new document is not active")
        self._candidate_owned[header["document_identity"]] = _OwnedDocument(document, "")

    def invalidate_candidate_workspace(self) -> None:
        """Release/restart loses authority; existing documents remain untouched."""
        self._candidate_owned = {}
        self._candidate_active = None

    def begin_candidate(self, expected: dict[str, Any], task_id: str) -> dict[str, Any]:
        gateway = cast(Any, self)
        candidate: _Candidate | None = None
        try:
            if not hasattr(self, "_candidate_root"):
                raise ValueError("candidate workspace is not configured by the host")
            if self._candidate_active is not None:
                raise ValueError("another candidate is still pending")
            if not isinstance(task_id, str) or not 1 <= len(task_id) <= 128 or "\0" in task_id:
                raise ValueError("task id is invalid")
            baseline = self._candidate_observe(expected, clean=True)
            source = gateway.require_document(part=True)
            header = self._candidate_header(source)
            owned = self._candidate_owned.get(header["document_identity"])
            if owned is None or owned.path != header["document_path"]:
                raise ValueError("source is not a task-created document in this worker")
            if owned.task_id not in {None, task_id}:
                raise ValueError("source belongs to a different task")
            stamp = self._candidate_stamp(source)
            geometry = self._candidate_geometry(source)
            if self._candidate_stamp(source) != stamp:
                raise ValueError("source changed while capturing checkpoint geometry")
            # Reads and filesystem preparation cannot authorize a stale source.
            self._candidate_observe(baseline, clean=True)
            task_dir = self._candidate_root / hashlib.sha256(task_id.encode()).hexdigest()[:24]
            task_dir.mkdir(exist_ok=True)
            if not task_dir.resolve().is_relative_to(self._candidate_root):
                raise ValueError("candidate path escaped the host workspace")
            token = uuid.uuid4().hex
            directory = task_dir / token
            directory.mkdir(exist_ok=False)
            path = directory / f"candidate-{token}.sldprt"
            if path.exists() or not directory.resolve().is_relative_to(self._candidate_root):
                raise ValueError("candidate destination is not a new owned path")
            candidate = _Candidate(token, task_id, source, header, stamp, baseline,
                                   geometry, self._candidate_context(), path)
            self._candidate_assert_source(candidate)
            owned.task_id = task_id
            owned.observation, owned.geometry = copy.deepcopy(baseline), copy.deepcopy(geometry)
            owned.header, owned.stamp = copy.deepcopy(header), stamp
            self._candidate_save_copy(source, path)
            # COPY must not rename, rebuild, switch or mutate the source.
            self._candidate_assert_source(candidate)
            self._candidate_observe(baseline, clean=True)
            opened = self._candidate_open(path, baseline["configuration"])
            candidate.document = opened
            candidate.header = self._candidate_header(opened)
            if candidate.header["document_identity"] == header["document_identity"]:
                raise ValueError("copy did not produce an independent document")
            if Path(candidate.header["document_path"]).resolve() != path.resolve():
                raise ValueError("opened document is not the new candidate file")
            active_id = gateway.probe_document_identity()["document_identity"]
            if active_id not in {header["document_identity"], candidate.header["document_identity"]}:
                raise ValueError("active document changed outside the candidate workflow")
            if active_id != candidate.header["document_identity"]:
                self._candidate_activate(opened)
            candidate_stamp = self._candidate_stamp(opened)
            observation = self._candidate_observe(clean=True)
            if not _equivalent(_state(baseline, identity=False), _state(observation, identity=False)):
                raise ValueError("native copy observation differs from the baseline")
            if not _equivalent(geometry, self._candidate_geometry(opened)):
                raise ValueError("native copy geometry differs from the baseline")
            if (self._candidate_stamp(opened) != candidate_stamp
                    or self._candidate_header(opened) != candidate.header):
                raise ValueError("candidate changed during native-copy verification")
            self._candidate_assert_source(candidate)
            gateway._reset_modeling_context("A new isolated candidate document is active.")
            candidate.last_observed_stamp = candidate_stamp
            self._candidate_active = candidate
            return {"ok": True, "status": "begun", "token": token,
                    "baseline": baseline, "candidate": observation,
                    "observation": observation, "artifact_path": str(path),
                    "recovery_mode": "activate_preserved_baseline"}
        except Exception as exc:
            result: dict[str, Any] = {"ok": False, "status": "unavailable", "reason": str(exc)}
            if candidate is not None:
                # OpenDoc6 can activate a valid copy yet report a warning.  Its
                # unique host path is sufficient to identify that retained copy
                # for a safe return, never an arbitrary currently active part.
                if candidate.header is None:
                    try:
                        current = gateway.require_document(part=True)
                        current_header = self._candidate_header(current)
                        if (current_header["document_path"]
                                and Path(current_header["document_path"]).resolve() == candidate.path.resolve()):
                            candidate.document, candidate.header = current, current_header
                    except Exception:
                        pass
                result["artifact_path"] = str(candidate.path)
                result["recovery"] = self._candidate_restore(candidate)
                if result["recovery"]["ok"]:
                    result["observation"] = result["recovery"]["observation"]
                    result["baseline_unchanged"] = True
            return result

    def finish_candidate(self, token: str, accept: bool,
                         expected: dict[str, Any]) -> dict[str, Any]:
        candidate = getattr(self, "_candidate_active", None)
        if candidate is None or not isinstance(token, str) or token != candidate.token:
            return {"ok": False, "status": "unavailable", "reason": "unknown or expired candidate token"}
        if not isinstance(accept, bool):
            return {"ok": False, "status": "unavailable", "reason": "accept must be boolean"}
        try:
            gateway = cast(Any, self)
            if self._candidate_header(gateway.require_document(part=True)) != candidate.header:
                raise ValueError("active candidate identity or configuration changed")
            candidate_stamp = self._candidate_stamp(candidate.document)
            observation = self._candidate_observe(expected, clean=accept)
            self._candidate_assert_source(candidate)
            if accept:
                # This checks supported geometry, but does NOT replace task acceptance.
                geometry = self._candidate_geometry(candidate.document)
                if (self._candidate_stamp(candidate.document) != candidate_stamp
                        or self._candidate_header(candidate.document) != candidate.header):
                    raise ValueError("candidate changed while committing")
                self._candidate_assert_source(candidate)
                assert candidate.header is not None
                checkpoint = self._candidate_persist(candidate, observation, geometry, candidate_stamp)
                self._candidate_owned[candidate.header["document_identity"]] = _OwnedDocument(
                    # SOLIDWORKS normalizes native extensions (for example
                    # .sldprt -> .SLDPRT). Ownership must retain the verified
                    # native path, not the requested spelling of the save path.
                    candidate.document, candidate.header["document_path"], candidate.task_id,
                    checkpoint["checkpoint_id"], copy.deepcopy(observation), copy.deepcopy(geometry),
                    copy.deepcopy(candidate.header), candidate_stamp,
                )
                self._candidate_active = None
                return {"ok": True, "status": "committed", "observation": observation,
                        "artifact_path": str(candidate.path), "persisted_current_state": True,
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "checkpoint_path": checkpoint["checkpoint_path"],
                        "checkpoint_parent_id": checkpoint["parent_id"]}
            result = self._candidate_restore(candidate)
            if result["ok"]:
                self._candidate_active = None
            return result
        except Exception as exc:
            # Do not activate any document after an external change/stale request.
            return {"ok": False, "status": "unavailable", "reason": str(exc),
                    "artifact_path": str(candidate.path)}

    def _candidate_store(self) -> CheckpointStore:
        if not hasattr(self, "_candidate_root"):
            raise ValueError("candidate workspace is not configured by the host")
        return CheckpointStore(self._candidate_root)

    def _candidate_persist(self, candidate: _Candidate, observation: dict[str, Any],
                           geometry: dict[str, Any], stamp: int) -> dict[str, Any]:
        """Save the final in-memory candidate, not its initial on-disk seed."""
        store = self._candidate_store()
        checkpoint_id, path = store.reserve(candidate.task_id)
        self._candidate_observe(observation, clean=True)
        self._candidate_assert_source(candidate)
        self._candidate_save_copy(candidate.document, path)
        if (self._candidate_header(candidate.document) != candidate.header
                or self._candidate_stamp(candidate.document) != stamp):
            raise ValueError("candidate changed during durable checkpoint save")
        self._candidate_assert_source(candidate)
        self._candidate_observe(observation, clean=True)
        if not _equivalent(geometry, self._candidate_geometry(candidate.document)):
            raise ValueError("candidate geometry changed during durable checkpoint save")
        self._candidate_observe(observation, clean=True)
        if (self._candidate_header(candidate.document) != candidate.header
                or self._candidate_stamp(candidate.document) != stamp):
            raise ValueError("candidate changed during durable checkpoint verification")
        self._candidate_assert_source(candidate)
        source_owned = self._candidate_owned[candidate.source_header["document_identity"]]
        assert candidate.header is not None
        manifest = store.publish(
            candidate.task_id, checkpoint_id, parent_id=source_owned.checkpoint_id,
            observation=observation, geometry=geometry, header=candidate.header, stamp=stamp,
        )
        # Disk hashing/fsync can take time. Publication preserves a verified
        # checkpoint, but must not authorize a now-changed active document.
        self._candidate_observe(observation, clean=True)
        if self._candidate_stamp(candidate.document) != stamp:
            raise ValueError("candidate changed while publishing its checkpoint")
        self._candidate_assert_source(candidate)
        return {**manifest, "checkpoint_path": str(path)}

    def checkpoint_request(self, operation: str, task_id: str,
                           checkpoint_id: str | None = None) -> dict[str, Any]:
        """Explicit host RPC. Listing is read-only; restore always creates a copy.

        After a worker restart, an already-open old document is *not* adopted.
        Restore then requires no active document. Other open documents are never
        saved, selected, changed, closed, or deleted by this method.
        """
        artifact: Path | None = None
        try:
            validate_task(task_id)
            store = self._candidate_store()
            if operation == "list":
                if checkpoint_id is not None:
                    raise ValueError("list does not accept a checkpoint id")
                return store.list(task_id)
            if operation != "restore":
                raise ValueError("checkpoint operation is not supported")
            if not isinstance(checkpoint_id, str):
                raise ValueError("checkpoint id is invalid")
            validate_checkpoint(checkpoint_id)
            if getattr(self, "_candidate_active", None) is not None:
                raise ValueError("an unfinished candidate is still active; checkpoint restore is refused")
            gateway = cast(Any, self)
            current = _read(gateway._sw, "ActiveDoc")
            boundary = self._checkpoint_restore_boundary(current, task_id)
            manifest = store.read(task_id, checkpoint_id)
            _clean(manifest["observation"])
            # Check source evidence consistency before any SolidWorks write.
            expected = manifest["observation"]
            saved_header = manifest["source_header"]
            if (saved_header.get("document_identity") != expected["document_identity"]
                    or saved_header.get("configuration") != expected["configuration"]
                    or saved_header.get("configuration_identity") != expected["configuration_identity"]
                    or saved_header.get("document_path") != expected["snapshot"]["document_path"]
                    or saved_header.get("document_title") != expected["snapshot"]["document_title"]
                    or saved_header.get("document_type") != expected["snapshot"]["document_type"]):
                raise ValueError("checkpoint source header and observation differ")
            if ("model_update_stamp" in expected
                    and expected["model_update_stamp"] != manifest["source_stamp"]):
                raise ValueError("checkpoint source stamp and observation differ")
            artifact = store.make_restore_copy(task_id, checkpoint_id, manifest)
            self._checkpoint_assert_boundary(current, boundary)
            opened = self._candidate_open(artifact, expected["configuration"])
            header = self._candidate_header(opened)
            if (not header["document_path"]
                    or Path(header["document_path"]).resolve() != artifact.resolve()
                    or (boundary is not None and header["document_identity"] == boundary["header"]["document_identity"])):
                raise ValueError("checkpoint restore did not open an independent working copy")
            active = _read(gateway._sw, "ActiveDoc")
            allowed = {header["document_identity"]}
            if boundary is not None:
                allowed.add(boundary["header"]["document_identity"])
            if active is not None and gateway._com_identity(active) not in allowed:
                raise ValueError("external document became active during checkpoint restore")
            if active is None or gateway._com_identity(active) != header["document_identity"]:
                self._candidate_activate(opened)
            stamp = self._candidate_stamp(opened)
            observation = self._candidate_observe(clean=True)
            geometry = self._candidate_geometry(opened)
            if not _equivalent(_state(expected, identity=False), _state(observation, identity=False)):
                raise ValueError("restored checkpoint observation differs from recorded evidence")
            if not _equivalent(manifest["geometry"], geometry):
                raise ValueError("restored checkpoint geometry differs from recorded evidence")
            self._candidate_observe(observation, clean=True)
            if self._candidate_header(opened) != header or self._candidate_stamp(opened) != stamp:
                raise ValueError("restored working copy changed during verification")
            self._checkpoint_assert_boundary(current, boundary, active=False)
            # Neither disk file may drift during native reopening/readback.
            if store.read(task_id, checkpoint_id) != manifest:
                raise ValueError("checkpoint manifest changed during native restore")
            if store.digest(artifact) != (manifest["native_sha256"], manifest["native_size"]):
                raise ValueError("restored file changed during native restore")
            self._candidate_observe(observation, clean=True)
            if self._candidate_stamp(opened) != stamp:
                raise ValueError("restored working copy changed during final disk verification")
            self._checkpoint_assert_boundary(current, boundary, active=False)
            gateway._reset_modeling_context("An independently verified checkpoint copy is active.")
            self._candidate_owned[header["document_identity"]] = _OwnedDocument(
                opened, header["document_path"], task_id, checkpoint_id,
                copy.deepcopy(observation), copy.deepcopy(geometry), copy.deepcopy(header), stamp,
            )
            return {"ok": True, "status": "restored", "checkpoint_id": checkpoint_id,
                    "checkpoint_parent_id": manifest["parent_id"], "observation": observation,
                    "artifact_path": str(artifact), "checkpoint_preserved": True,
                    "recovery_mode": "verified_durable_checkpoint_copy"}
        except Exception as exc:
            result: dict[str, Any] = {"ok": False, "status": "unavailable", "reason": str(exc)}
            if artifact is not None:
                result.update(artifact_path=str(artifact), working_copy_preserved=True)
            return result

    def _checkpoint_restore_boundary(self, current: Any, task_id: str) -> dict[str, Any] | None:
        if current is None:
            return None
        header = self._candidate_header(current)
        owned = self._candidate_owned.get(header["document_identity"])
        if (owned is None or owned.task_id != task_id or owned.path != header["document_path"]
                or owned.header != header or owned.stamp != self._candidate_stamp(current)
                or owned.observation is None or owned.geometry is None):
            raise ValueError("active document is not an unchanged task-owned checkpoint boundary")
        observation = self._candidate_observe(owned.observation, clean=True)
        geometry = self._candidate_geometry(current)
        if not _equivalent(owned.geometry, geometry):
            raise ValueError("active task document geometry changed outside the worker")
        return {"header": header, "stamp": owned.stamp, "observation": observation, "geometry": geometry}

    def _checkpoint_assert_boundary(self, current: Any, boundary: dict[str, Any] | None,
                                    *, active: bool = True) -> None:
        gateway = cast(Any, self)
        active_document = _read(gateway._sw, "ActiveDoc")
        if boundary is None:
            if active and active_document is not None:
                raise ValueError("a document became active before checkpoint restore")
            return
        if (self._candidate_header(current) != boundary["header"]
                or self._candidate_stamp(current) != boundary["stamp"]):
            raise ValueError("prior task document changed during checkpoint restore")
        names: list[tuple[str, str]] = []
        feature = _read(current, "FirstFeature")
        while feature is not None:
            if len(names) >= 3000:
                raise ValueError("prior task document feature traversal is incomplete")
            names.append((_read(feature, "Name"), _read(feature, "GetTypeName2")))
            feature = _read(feature, "GetNextFeature")
        expected = [(item["name"], item["type"]) for item in boundary["observation"]["features"]]
        if names != expected:
            raise ValueError("prior task document feature tree changed during restore")
        if active and (active_document is None
                       or gateway._com_identity(active_document) != boundary["header"]["document_identity"]):
            raise ValueError("active task document changed before checkpoint restore")

    def _candidate_header(self, document: Any) -> dict[str, Any]:
        gateway = cast(Any, self)
        config = _read(_read(document, "ConfigurationManager"), "ActiveConfiguration")
        result = {"document_identity": gateway._com_identity(document),
                  "document_title": _read(document, "GetTitle"),
                  "document_path": _read(document, "GetPathName"),
                  "document_type": "part" if _read(document, "GetType") == 1 else "other",
                  "configuration": _read(config, "Name"),
                  "configuration_identity": gateway._com_identity(config)}
        if any(not isinstance(value, str) or "\0" in value for value in result.values()):
            raise ValueError("document/configuration identity is unavailable")
        if not all(result[name] for name in result if name != "document_path"):
            raise ValueError("document/configuration identity is empty")
        return result

    def candidate_write_guard(self, document: Any = None, *, check_stamp: bool = False) -> None:
        """Verify the native target immediately before a host-approved CAD write.

        The gateway calls this at the top-level write boundary with stamp checking
        and again whenever a write helper obtains its document.  Internal calls
        skip stamp comparison because the current command may already have made
        its own verified changes; they still cannot adopt another document.
        This is a guard, not a lock against other external COM clients.
        """
        try:
            self._candidate_write_guard_checked(document, check_stamp=check_stamp)
        except Exception as exc:
            raise CandidateStateChanged(str(exc)) from exc

    def _candidate_write_guard_checked(self, document: Any = None, *, check_stamp: bool = False) -> None:
        candidate = getattr(self, "_candidate_active", None)
        if candidate is None:
            return
        gateway = cast(Any, self)
        current = _read(gateway._sw, "ActiveDoc")
        if current is None or self._candidate_header(current) != candidate.header:
            raise ValueError("active document or configuration is no longer the candidate")
        if document is not None and gateway._com_identity(document) != gateway._com_identity(candidate.document):
            raise ValueError("write helper obtained a document outside the candidate")
        if (self._candidate_header(candidate.source) != candidate.source_header
                or self._candidate_stamp(candidate.source) != candidate.source_stamp):
            raise ValueError("preserved baseline changed outside the experiment")
        if check_stamp and (candidate.last_observed_stamp is None
                            or self._candidate_stamp(current) != candidate.last_observed_stamp):
            raise ValueError("candidate changed since the last complete native observation")

    @staticmethod
    def _candidate_stamp(document: Any) -> int:
        return _integer(_read(document, "GetUpdateStamp"), "document update stamp")

    def _candidate_observe(self, expected: dict[str, Any] | None = None,
                           *, clean: bool = False) -> dict[str, Any]:
        gateway = cast(Any, self)
        document = gateway.require_document(part=True)
        stamp = self._candidate_stamp(document)
        header = self._candidate_header(document)
        observation = gateway._observe_model(include_configuration=True)
        state = _state(observation)
        if (self._candidate_stamp(document) != stamp or self._candidate_header(document) != header
                or gateway.probe_document_identity()["document_identity"] != header["document_identity"]):
            raise ValueError("document changed during observation")
        if expected is not None and not _equivalent(state, _state(expected)):
            raise ValueError("observation changed since the host verified it")
        if expected is not None and "model_update_stamp" in expected:
            if _integer(expected["model_update_stamp"], "expected update stamp") != stamp:
                raise ValueError("model update stamp changed since the host verified it")
        if "model_update_stamp" in observation and observation["model_update_stamp"] != stamp:
            raise ValueError("observation update stamp differs from the native document")
        if clean:
            _clean(observation)
        candidate = getattr(self, "_candidate_active", None)
        if candidate is not None and candidate.header == header:
            candidate.last_observed_stamp = stamp
        return observation

    def _candidate_geometry(self, document: Any) -> dict[str, Any]:
        """Read all supported sketches and exact body extents, without selection."""
        gateway = cast(Any, self)
        stamp = self._candidate_stamp(document)
        header = self._candidate_header(document)
        if _integer(_read(document, "GetConfigurationCount"), "configuration count") != 1:
            raise ValueError("candidate workflow currently requires one configuration")
        extension = _read(document, "Extension")
        if _integer(_read(extension, "ListExternalFileReferencesCount"), "external references"):
            raise ValueError("native-copy experiments do not support external references")
        if _integer(_read(document, "ListAuxiliaryExternalFileReferencesCount"), "auxiliary references"):
            raise ValueError("native-copy experiments do not support auxiliary references")
        sketches: list[dict[str, Any]] = []
        visited: set[str] = set()

        def walk(first: Any, *, sub: bool = False, depth: int = 0) -> None:
            if depth > 32:
                raise ValueError("feature hierarchy exceeds candidate limit")
            feature = first
            chain: set[str] = set()
            while feature is not None:
                identity = gateway._com_identity(feature)
                if identity in chain:
                    raise ValueError("cyclic feature hierarchy")
                chain.add(identity)
                if len(visited) >= 3000:
                    raise ValueError("feature count exceeds candidate limit")
                if identity not in visited:
                    visited.add(identity)
                    kind = _read(feature, "GetTypeName2")
                    if kind in {"ProfileFeature", "3DProfileFeature"}:
                        sketch = _read(feature, "GetSpecificFeature2")
                        if _read(sketch, "Is3D") not in {False, 0}:
                            raise ValueError("3D sketch fidelity is not supported")
                        segments = _read(sketch, "GetSketchSegments")
                        if segments is None:
                            segments = []
                        if not isinstance(segments, (tuple, list)) or len(segments) > 512:
                            raise ValueError("sketch segment evidence is unavailable or too large")
                        geometry = [gateway._segment_geometry(item) for item in segments]
                        geometry.sort(key=lambda item: json.dumps(item, sort_keys=True, allow_nan=False))
                        transform = _read(_read(sketch, "ModelToSketchTransform"), "ArrayData")
                        if not isinstance(transform, (tuple, list)) or len(transform) != 16:
                            raise ValueError("sketch coordinate transform is unavailable")
                        sketches.append({"name": _read(feature, "Name"), "segments": geometry,
                                         "transform": list(transform)})
                    walk(_read(feature, "GetFirstSubFeature"), sub=True, depth=depth + 1)
                feature = _read(feature, "GetNextSubFeature" if sub else "GetNextFeature")

        walk(_read(document, "FirstFeature"))
        extents = gateway._measure_extents()
        if extents.get("complete") is not True or extents.get("issues") != []:
            # Empty fresh parts have no extents; an explicit zero body count is
            # the only valid empty baseline, never an unreadable nonempty part.
            bodies = _read(document, "GetBodies2", 0, False)
            if bodies is not None and bodies != () and bodies != []:
                raise ValueError("complete solid extents are required")
            extent_state: dict[str, Any] = {"body_count": 0}
        else:
            extent_state = {name: value for name, value in extents.items()
                            if name not in {"document_identity", "document_title", "document_path",
                                            "configuration_identity", "captured_at"}}
        result = {"sketches": sorted(sketches, key=lambda item: item["name"]), "extents": extent_state}
        serialized = json.dumps(result, allow_nan=False)
        if len(serialized.encode("utf-8")) > 1_000_000:
            raise ValueError("candidate geometry evidence exceeds the local limit")
        if (self._candidate_stamp(document) != stamp or self._candidate_header(document) != header
                or gateway.probe_document_identity()["document_identity"] != header["document_identity"]):
            raise ValueError("document changed during geometry capture")
        return result

    def _candidate_assert_source(self, candidate: _Candidate) -> None:
        if (self._candidate_header(candidate.source) != candidate.source_header
                or self._candidate_stamp(candidate.source) != candidate.source_stamp):
            raise ValueError("preserved baseline changed outside the experiment")
        # GetUpdateStamp explicitly does not cover feature renaming.  Check the
        # ordered native tree as well, without activating or selecting anything.
        names: list[tuple[str, str]] = []
        feature = _read(candidate.source, "FirstFeature")
        while feature is not None:
            if len(names) >= 3000:
                raise ValueError("preserved baseline feature traversal is incomplete")
            names.append((_read(feature, "Name"), _read(feature, "GetTypeName2")))
            feature = _read(feature, "GetNextFeature")
        expected = [(item["name"], item["type"])
                    for item in candidate.source_observation["features"]]
        if names != expected:
            raise ValueError("preserved baseline feature tree changed outside the experiment")

    def _candidate_context(self) -> dict[str, Any]:
        return {name: copy.copy(getattr(self, name)) for name in _CONTEXT_FIELDS
                if hasattr(self, name)}

    def _candidate_restore(self, candidate: _Candidate) -> dict[str, Any]:
        gateway = cast(Any, self)
        try:
            self._candidate_assert_source(candidate)
            active_id = gateway.probe_document_identity()["document_identity"]
            permitted = {candidate.source_header["document_identity"]}
            if candidate.header is not None:
                permitted.add(candidate.header["document_identity"])
            if active_id not in permitted:
                raise ValueError("an external document is active; automatic switching is refused")
            if active_id != candidate.source_header["document_identity"]:
                self._candidate_activate(candidate.source)
            self._candidate_assert_source(candidate)
            observation = self._candidate_observe(candidate.source_observation, clean=True)
            if not _equivalent(candidate.source_geometry, self._candidate_geometry(candidate.source)):
                raise ValueError("preserved baseline geometry changed")
            for name, value in candidate.source_context.items():
                setattr(self, name, copy.copy(value))
            return {"ok": True, "status": "restored", "observation": observation,
                    "artifact_path": str(candidate.path), "candidate_preserved": True,
                    "native_context_restored": True,
                    "recovery_mode": "activate_preserved_baseline"}
        except Exception as exc:
            return {"ok": False, "status": "unavailable", "reason": str(exc),
                    "artifact_path": str(candidate.path)}

    def _candidate_save_copy(self, document: Any, path: Path) -> None:
        gateway = cast(Any, self)
        validate_native_path(path)
        if gateway.probe_document_identity()["document_identity"] != gateway._com_identity(document):
            raise ValueError("source is no longer active before saving its copy")
        if path.exists() or not path.parent.resolve().is_relative_to(self._candidate_root):
            raise ValueError("candidate save destination is no longer a new owned path")
        success, codes = self._candidate_invoke(
            _read(document, "Extension"), "SaveAs3", str(path), 0, 11, None, None
        )
        if success is not True or any(code != 0 for code in codes):
            raise ValueError(f"native copy save failed or reported warnings (success={success!r}, "
                             f"errors={codes[0]}, warnings={codes[1]})")
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("native copy artifact is missing or empty")

    def _candidate_open(self, path: Path, configuration: str) -> Any:
        gateway = cast(Any, self)
        validate_native_path(path)
        document, codes = self._candidate_invoke(gateway._sw, "OpenDoc6", str(path), 1, 1, configuration)
        if document is None or any(code != 0 for code in codes):
            raise ValueError("candidate native file could not be opened cleanly")
        return document

    def _candidate_activate(self, document: Any) -> None:
        gateway = cast(Any, self)
        identity = gateway._com_identity(document)
        # Saved documents use their unique full pathname; unsaved source names
        # are additionally validated against the returned native COM identity.
        name = _read(document, "GetPathName") or _read(document, "GetTitle")
        activated, codes = self._candidate_invoke(gateway._sw, "ActivateDoc3", name, False, 1)
        if (activated is None or any(code != 0 for code in codes)
                or gateway._com_identity(activated) != identity
                or gateway.probe_document_identity()["document_identity"] != identity):
            raise ValueError("requested document could not be activated without rebuild")

    def _candidate_invoke(self, obj: Any, name: str, *arguments: Any) -> tuple[Any, list[int]]:
        """One typed dispatch, independent of generated/late-bound wrappers."""
        if name not in _CANDIDATE_COM_SIGNATURES:
            raise ValueError("candidate COM method is not allowlisted")
        return_type, argument_types, input_count, output_count = _CANDIDATE_COM_SIGNATURES[name]
        if len(arguments) != input_count:
            raise ValueError("candidate COM method argument count differs")
        ole = getattr(obj, "_oleobj_", None)
        if ole is None:
            # Plain deterministic test doubles retain their ordinary Python
            # call contract. Real COM objects always use the single typed path.
            references = [cast(Any, self)._int_reference() for _ in range(output_count)]
            return self._candidate_com_result(getattr(obj, name)(*arguments, *references), *references)
        result = ole.InvokeTypes(
            ole.GetIDsOfNames(name), 0, 1, return_type, argument_types,
            *arguments, *([0] * output_count),
        )
        if not isinstance(result, (tuple, list)) or len(result) != output_count + 1:
            raise ValueError("typed candidate COM call did not return complete status outputs")
        value, *outputs = result
        codes = [_integer(item, "typed COM status") for item in outputs]
        if name != "SaveAs3" and value is not None:
            from win32com.client import Dispatch

            value = Dispatch(value)
        return value, codes

    @staticmethod
    def _candidate_com_result(result: Any, *references: Any) -> tuple[Any, list[int]]:
        if isinstance(result, (tuple, list)):
            if len(result) != len(references) + 1:
                raise ValueError("unexpected COM result layout")
            document, *raw_codes = result
        else:
            document = result
            raw_codes = [getattr(reference, "value", None) for reference in references]
        return document, [_integer(code, "COM status") for code in raw_codes]
