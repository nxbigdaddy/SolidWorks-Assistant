"""Read-only source/runtime fingerprints shared by offline and opt-in CAD tests.

Never read runtime settings, credentials, logs, task records or CAD documents.
Model/Pi/SolidWorks identifiers must be supplied by the authorized collector.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import tomllib
from pathlib import Path
from typing import Any


def source_fingerprint(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    fixed = ("pyproject.toml", "launcher.py", "requirements.txt", "requirements-validated.txt")
    files = [root / name for name in fixed if (root / name).is_file()]
    for directory in ("src", "scripts"):
        files.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and path.suffix.casefold() in {".py", ".mjs", ".js", ".ps1"})
    hashes = {}
    for path in sorted(set(files)):
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("source file resolves outside the selected repository")
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "pyproject.toml" not in hashes or not any(name.startswith("src/") for name in hashes):
        raise ValueError("source fingerprint requires pyproject.toml and source files")
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    def git(*arguments: str) -> str:
        # Only fixed read-only commands; no shell interpolation or user arguments.
        result = subprocess.run(["git", "-C", str(root), *arguments], check=True,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        return result.stdout.strip()

    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=normal", "--",
                     "src", "scripts", *fixed))
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"assistant_version": str(version), "git_head": head, "git_dirty": dirty,
            "files_sha256": hashes, "source_tree_sha256": hashlib.sha256(encoded).hexdigest()}


def runtime_fingerprint(*, solidworks_version: str, pi_version: str, model_id: str,
                        model_settings_sha256: str, cad_mode: str) -> dict[str, str]:
    try:
        pywin32 = importlib.metadata.version("pywin32")
    except importlib.metadata.PackageNotFoundError:
        pywin32 = "not-installed"
    return {"python_version": platform.python_version(), "pywin32_version": pywin32,
            "os_version": f"{platform.system()} {platform.release()} {platform.version()} {platform.machine()}",
            "solidworks_version": solidworks_version, "pi_version": pi_version,
            "model_id": model_id, "model_settings_sha256": model_settings_sha256, "cad_mode": cad_mode}


def benchmark_environment(source: dict[str, Any], runtime: dict[str, str]) -> dict[str, str]:
    """Flatten the shared report fields into the scorer's strict cohort contract."""
    return {"source_revision": source["git_head"], "source_tree_sha256": source["source_tree_sha256"],
            "assistant_version": source["assistant_version"], **runtime}
