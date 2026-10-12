from __future__ import annotations

import threading

import pytest

from sw_assistant.domain import ModelSnapshot, ToolError
from sw_assistant.library import DerivationService

from fakes import FakeWorker, result


def test_byte_copy_is_hash_verified_and_never_overwrites(tmp_path):
    source = tmp_path / "source.SLDPRT"
    destination = tmp_path / "copy.SLDPRT"
    source.write_bytes(b"solidworks-part")
    service = DerivationService(FakeWorker())
    copied = service.copy_part(source, destination)
    assert destination.read_bytes() == source.read_bytes()
    assert copied.source_sha256 == copied.destination_sha256
    with pytest.raises(ToolError, match="已存在"):
        service.copy_part(source, destination)


def test_plain_assembly_copy_is_rejected_for_pack_and_go(tmp_path):
    source = tmp_path / "source.SLDASM"
    source.write_bytes(b"assembly")
    with pytest.raises(ToolError, match="Pack and Go"):
        DerivationService(FakeWorker()).copy_part(source, tmp_path / "copy.SLDASM")


def test_parameter_derivation_only_publishes_verified_staging_copy(tmp_path):
    source = tmp_path / "source.SLDPRT"
    destination = tmp_path / "derived.SLDPRT"
    source.write_bytes(b"solidworks-part")
    manifest = {
        "schema_version": 1,
        "dimensions": [{"name": "D1@Sketch1", "display_value_mm": 25.0}],
    }
    worker = FakeWorker(
        [
            result(
                before=ModelSnapshot(True),
                after=ModelSnapshot(True),
                data={"manifest": manifest},
            )
        ]
    )
    derived = DerivationService(worker).derive_part(
        source, destination, {"D1@Sketch1": 25}
    )
    assert destination.read_bytes() == source.read_bytes()
    assert derived.manifest == manifest
    assert worker.calls[0].tool == "library.modify_part_copy"
    assert worker.calls[0].arguments["path"] != str(source)
    assert not list(tmp_path.glob(".*.sw-assistant-*.SLDPRT"))


def test_failed_parameter_derivation_leaves_no_target_or_staging(tmp_path):
    source = tmp_path / "source.SLDPRT"
    destination = tmp_path / "derived.SLDPRT"
    source.write_bytes(b"solidworks-part")
    worker = FakeWorker([result(ok=False, message="rebuild failed")])
    with pytest.raises(ToolError, match="rebuild failed"):
        DerivationService(worker).derive_part(source, destination, {"D1@Sketch1": 25})
    assert not destination.exists()
    assert not list(tmp_path.glob(".*.sw-assistant-*.SLDPRT"))


def test_cancelled_copy_never_leaves_partial_target(tmp_path):
    source = tmp_path / "source.SLDPRT"
    destination = tmp_path / "copy.SLDPRT"
    source.write_bytes(b"solidworks-part")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError, match="停止"):
        DerivationService(FakeWorker()).copy_part(
            source, destination, cancel=cancel
        )
    assert not destination.exists()
