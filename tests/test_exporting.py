from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sw_assistant.domain import ModelSnapshot, StepResult, ToolError
from sw_assistant.library import ExportService
from sw_assistant.solidworks.gateway import SolidWorksGateway


class _ExportWorker:
    def __init__(self, *, fail=False, empty=False) -> None:
        self.fail = fail
        self.empty = empty
        self.calls = []

    def call(self, call, timeout_seconds=120):
        self.calls.append(call)
        if not self.fail:
            Path(call.arguments["destination"]).write_bytes(
                b"" if self.empty else b"converted-model"
            )
        return StepResult(
            not self.fail,
            call,
            "export failed" if self.fail else "ok",
            before=ModelSnapshot(True),
            after=ModelSnapshot(True),
        )


def test_export_publishes_verified_file_and_preserves_source(tmp_path):
    source = tmp_path / "Part.SLDPRT"
    destination = tmp_path / "Part.STEP"
    source.write_bytes(b"native-part")
    worker = _ExportWorker()

    result = ExportService(worker).export(source, destination)

    assert source.read_bytes() == b"native-part"
    assert destination.read_bytes() == b"converted-model"
    assert result.destination_sha256
    assert worker.calls[0].tool == "document.export"
    assert not list(tmp_path.glob(".*.sw-export-*"))


@pytest.mark.parametrize(
    ("source_name", "destination_name"),
    [("Part.SLDPRT", "Part.PDF"), ("Drawing.SLDDRW", "Drawing.STEP")],
)
def test_export_rejects_format_mismatch(tmp_path, source_name, destination_name):
    source = tmp_path / source_name
    source.write_bytes(b"fixture")
    with pytest.raises(ToolError, match="允许格式"):
        ExportService(_ExportWorker()).export(source, tmp_path / destination_name)


def test_export_failure_empty_output_and_cancel_leave_no_artifact(tmp_path):
    source = tmp_path / "Part.SLDPRT"
    source.write_bytes(b"fixture")
    for worker in (_ExportWorker(fail=True), _ExportWorker(empty=True)):
        destination = tmp_path / f"{id(worker)}.STEP"
        with pytest.raises(ToolError):
            ExportService(worker).export(source, destination)
        assert not destination.exists()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError, match="停止"):
        ExportService(_ExportWorker()).export(
            source, tmp_path / "cancel.STEP", cancel=cancel
        )


class _ExportExtension:
    def __init__(self, destination_bytes=b"converted", result=(True, 0, 0)) -> None:
        self.destination_bytes = destination_bytes
        self.result = result
        self.arguments = None

    def SaveAs3(self, *arguments):
        self.arguments = arguments
        Path(arguments[0]).write_bytes(self.destination_bytes)
        return self.result


class _ExportDocument:
    def __init__(self, extension, *, dirty=False) -> None:
        self.Extension = extension
        self.dirty = dirty

    @staticmethod
    def GetTitle():
        return "Part.SLDPRT"

    def GetSaveFlag(self):
        return self.dirty

    @staticmethod
    def ClearSelection2(_all):
        return None


def _gateway_with_open_document(document):
    application = type(
        "Application",
        (),
        {
            "ActiveDoc": document,
            "GetOpenDocumentByName": staticmethod(lambda _path: document),
            "ActivateDoc3": staticmethod(lambda *_args: (document, 0)),
        },
    )()
    gateway = SolidWorksGateway()
    gateway._sw = application
    return gateway


def test_gateway_exports_active_document_with_silent_save_as3(tmp_path):
    source = tmp_path / "Part.SLDPRT"
    destination = tmp_path / "Part.STEP"
    source.write_bytes(b"part")
    extension = _ExportExtension()
    gateway = _gateway_with_open_document(_ExportDocument(extension))

    gateway._export_document(str(source), str(destination))

    assert extension.arguments[:3] == (str(destination.resolve()), 0, 1)
    assert destination.read_bytes() == b"converted"


def test_gateway_rejects_dirty_document_and_nonzero_save_error(tmp_path):
    source = tmp_path / "Part.SLDPRT"
    source.write_bytes(b"part")
    with pytest.raises(ToolError, match="未保存修改"):
        _gateway_with_open_document(
            _ExportDocument(_ExportExtension(), dirty=True)
        )._export_document(str(source), str(tmp_path / "dirty.STEP"))

    extension = _ExportExtension(result=(True, 32, 0))
    with pytest.raises(ToolError, match="32"):
        _gateway_with_open_document(_ExportDocument(extension))._export_document(
            str(source), str(tmp_path / "error.STEP")
        )
