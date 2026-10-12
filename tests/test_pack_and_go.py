from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sw_assistant.domain import ModelSnapshot, StepResult, ToolError
from sw_assistant.library import PackAndGoService
from sw_assistant.solidworks.gateway import SolidWorksGateway


class _PackagingWorker:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def call(self, call, timeout_seconds=120):
        self.calls.append(call)
        if self.fail:
            return StepResult(
                False,
                call,
                "Pack and Go failed",
                before=ModelSnapshot(True),
                after=ModelSnapshot(True),
            )
        destination = Path(call.arguments["destination_dir"])
        (destination / "Assembly.SLDASM").write_bytes(b"assembly-copy")
        references = destination / "References"
        references.mkdir()
        (references / "Part1.SLDPRT").write_bytes(b"part-copy")
        return StepResult(
            True,
            call,
            "ok",
            before=ModelSnapshot(True),
            after=ModelSnapshot(True),
            data={"saved_paths": ["Assembly.SLDASM", "References/Part1.SLDPRT"]},
        )


def test_pack_and_go_publishes_validated_package_without_touching_source(tmp_path):
    source = tmp_path / "Assembly.SLDASM"
    destination = tmp_path / "Published"
    source.write_bytes(b"original-assembly")
    worker = _PackagingWorker()

    result = PackAndGoService(worker).package_assembly(source, destination)

    assert source.read_bytes() == b"original-assembly"
    assert (destination / "Assembly.SLDASM").read_bytes() == b"assembly-copy"
    assert result.file_count == 2
    assert result.files == ["Assembly.SLDASM", "References/Part1.SLDPRT"]
    assert result.destination_sha256
    assert worker.calls[0].tool == "assembly.pack_and_go"
    assert not list(tmp_path.glob(".Published.sw-pack-*"))


def test_failed_pack_and_go_leaves_no_target_or_staging(tmp_path):
    source = tmp_path / "Assembly.SLDASM"
    destination = tmp_path / "Published"
    source.write_bytes(b"original-assembly")

    with pytest.raises(ToolError, match="failed"):
        PackAndGoService(_PackagingWorker(fail=True)).package_assembly(
            source, destination
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".Published.sw-pack-*"))


def test_pack_and_go_never_overwrites_existing_directory(tmp_path):
    source = tmp_path / "Assembly.SLDASM"
    destination = tmp_path / "Published"
    source.write_bytes(b"original-assembly")
    destination.mkdir()

    with pytest.raises(ToolError, match="已经存在"):
        PackAndGoService(_PackagingWorker()).package_assembly(source, destination)


def test_cancelled_pack_and_go_never_creates_target(tmp_path):
    source = tmp_path / "Assembly.SLDASM"
    destination = tmp_path / "Published"
    source.write_bytes(b"original-assembly")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(InterruptedError, match="停止"):
        PackAndGoService(_PackagingWorker()).package_assembly(
            source, destination, cancel=cancel
        )

    assert not destination.exists()


class _Package:
    def __init__(self) -> None:
        self.save_to = None

    @staticmethod
    def GetDocumentNames():
        return (["original.SLDASM", "part.SLDPRT"], 2)

    def SetSaveToName(self, use_folder, path):
        self.save_to = (use_folder, path)
        return True

    @staticmethod
    def GetDocumentSaveToNames():
        return (["copy.SLDASM", "part.SLDPRT"], [0, 0])


class _PackExtension:
    def __init__(self, package, statuses=(0, 0)) -> None:
        self.package = package
        self.statuses = statuses

    def GetPackAndGo(self):
        return self.package

    def SavePackAndGo(self, package):
        assert package is self.package
        return self.statuses


class _PackDocument:
    def __init__(self, extension) -> None:
        self.Extension = extension

    @staticmethod
    def GetTitle():
        return "Assembly.SLDASM"


def test_gateway_configures_official_pack_and_go_and_checks_status(tmp_path):
    source = tmp_path / "Assembly.SLDASM"
    destination = tmp_path / "Published"
    source.write_bytes(b"assembly")
    destination.mkdir()
    package = _Package()
    document = _PackDocument(_PackExtension(package))
    gateway = SolidWorksGateway()
    gateway._sw = type(
        "Application",
        (),
        {"GetOpenDocumentByName": staticmethod(lambda _path: document)},
    )()

    result = gateway._pack_and_go(
        str(source),
        str(destination),
        include_drawings=True,
        include_toolbox=False,
    )

    assert package.save_to == (True, str(destination.resolve()))
    assert package.IncludeDrawings is True
    assert package.IncludeToolboxComponents is False
    assert result["statuses"] == [0, 0]


def test_gateway_rejects_nonzero_pack_and_go_status(tmp_path):
    source = tmp_path / "Assembly.SLDASM"
    destination = tmp_path / "Published"
    source.write_bytes(b"assembly")
    destination.mkdir()
    package = _Package()
    document = _PackDocument(_PackExtension(package, statuses=(0, 7)))
    gateway = SolidWorksGateway()
    gateway._sw = type(
        "Application",
        (),
        {"GetOpenDocumentByName": staticmethod(lambda _path: document)},
    )()

    with pytest.raises(ToolError, match="失败状态.*7"):
        gateway._pack_and_go(
            str(source),
            str(destination),
            include_drawings=True,
            include_toolbox=True,
        )
