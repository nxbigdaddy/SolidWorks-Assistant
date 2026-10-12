"""Exercise the real PowerShell source selector on a synthetic checkout."""
from __future__ import annotations

import os
import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "CODEX-HANDOFF.md", "README.md", "pyproject.toml", "src/sw_assistant/__init__.py",
    "src-dotnet/SolidWorksAssistant.EventBridge/SolidWorksAssistant.EventBridge.csproj",
    "tests/test_runtime.py", "docs/index.md", "installer/build_installer.ps1",
]


def checkout(tmp_path):
    root = tmp_path / "checkout"
    for name in REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic source", encoding="utf-8")
    script = root / "scripts" / "build_codex_handoff.ps1"
    script.parent.mkdir()
    shutil.copyfile(ROOT / "scripts" / "build_codex_handoff.ps1", script)
    return root, script


def package(root, script, output, *, data_root=None):
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell source packaging is a Windows release gate")
    environment = dict(os.environ)
    environment.pop("SW_ASSISTANT_DATA_DIR", None)
    # PowerShell 7's inherited module search path can shadow Windows PowerShell
    # 5.1 built-ins; let the selected host create its own standard module path.
    environment.pop("PSModulePath", None)
    if data_root is not None:
        environment["SW_ASSISTANT_DATA_DIR"] = str(data_root)
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-OutputPath", str(output)], cwd=root, env=environment, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30,
    )


def members(output):
    with zipfile.ZipFile(output) as archive:
        return {name.partition("/")[2] for name in archive.namelist()}


def test_source_archive_excludes_credentials_tasks_and_old_custom_data(tmp_path):
    root, script = checkout(tmp_path)
    private = [
        "settings.json", "settings.json.bak", "auth.local.json", "credentials.json",
        "token.cache.json", ".env.production", "tasks/task-id.json",
        "task-agent/task-123/context.txt", "pi-agent/session/input.txt",
        "sessions/session-id.json", "event-bridge/current-context.json",
        "custom-user-data/settings.json", "custom-user-data/notes/private.txt",
        "nested/app-data/library.db", "nested/app-data/unusual-extension.secret",
    ]
    for name in private:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic private data", encoding="utf-8")
    output = tmp_path / "source.zip"
    result = package(root, script, output)
    assert result.returncode == 0, result.stderr
    selected = members(output)
    assert set(REQUIRED) <= selected
    assert selected.isdisjoint(private)
    assert not any(name.startswith(("custom-user-data/", "nested/app-data/")) for name in selected)
    digest = hashlib.sha256(output.read_bytes()).hexdigest().upper()
    assert output.with_suffix(".zip.sha256").read_text("utf-8").startswith(digest + " *")
    with zipfile.ZipFile(output) as archive:
        manifest_name = next(name for name in archive.namelist() if name.endswith("MANIFEST.sha256"))
        prefix = manifest_name.rpartition("/")[0] + "/"
        for line in archive.read(manifest_name).decode("utf-8").splitlines():
            expected, relative = line.split(" *", 1)
            assert hashlib.sha256(archive.read(prefix + relative)).hexdigest().upper() == expected


def test_source_archive_excludes_configured_markerless_data_directory(tmp_path):
    root, script = checkout(tmp_path)
    data_root = root / "my-local-state"
    data_root.mkdir()
    (data_root / "private-context.txt").write_text("synthetic state", encoding="utf-8")
    output = tmp_path / "source.zip"
    result = package(root, script, output, data_root=data_root)
    assert result.returncode == 0, result.stderr
    assert "my-local-state/private-context.txt" not in members(output)


def test_source_packaging_rejects_runtime_data_mixed_into_project_root(tmp_path):
    root, script = checkout(tmp_path)
    output = tmp_path / "source.zip"
    result = package(root, script, output, data_root=root)
    assert result.returncode != 0
    assert "runtime data directory" in result.stderr
    assert not output.exists()
