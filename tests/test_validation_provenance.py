"""Shared fingerprint inspection stays in the selected source tree, not user data."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from sw_assistant.validation import provenance
from sw_assistant.validation.autonomy_benchmark import validate_environment


def test_source_fingerprint_covers_native_scripts_and_pi_assets_not_logs(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "test-1"\n', encoding="utf-8")
    (tmp_path / "src" / "pi_assets").mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text("synthetic = True\n", encoding="utf-8")
    pi = tmp_path / "src" / "pi_assets" / "provider.mjs"
    pi.write_text("export const synthetic = true;", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "verify.ps1").write_text("synthetic", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "private.json").write_text("not source", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[:3] == ["git", "-C", str(tmp_path)] and kwargs["timeout"] == 15
        return SimpleNamespace(stdout="a" * 40 if "rev-parse" in command else " M src/main.py")

    monkeypatch.setattr(provenance.subprocess, "run", fake_run)
    first = provenance.source_fingerprint(tmp_path)
    assert first["assistant_version"] == "test-1" and first["git_dirty"]
    assert first["files_sha256"]["src/pi_assets/provider.mjs"] == hashlib.sha256(pi.read_bytes()).hexdigest()
    assert not any("logs" in name for name in first["files_sha256"])
    assert "scripts/verify.ps1" in first["files_sha256"]
    pi.write_text("export const synthetic = false;", encoding="utf-8")
    second = provenance.source_fingerprint(tmp_path)
    assert first["git_head"] == second["git_head"]
    assert first["source_tree_sha256"] != second["source_tree_sha256"]
    assert len(calls) == 4


def test_runtime_uses_explicit_identifiers_and_shared_environment_fields(monkeypatch):
    monkeypatch.setattr(provenance.importlib.metadata, "version", lambda name: "fixture-pywin32")
    runtime = provenance.runtime_fingerprint(solidworks_version="fixture-sw", pi_version="fixture-pi",
                                            model_id="fixture-model", model_settings_sha256="a" * 64, cad_mode="synthetic")
    source = {"git_head": "b" * 40, "source_tree_sha256": "c" * 64, "assistant_version": "test-1"}
    value = provenance.benchmark_environment(source, runtime)
    assert validate_environment(value) == value
    assert value["pywin32_version"] == "fixture-pywin32"


def test_no_git_or_missing_source_does_not_invent_a_trusted_version(tmp_path):
    with pytest.raises(ValueError):
        provenance.source_fingerprint(tmp_path)
