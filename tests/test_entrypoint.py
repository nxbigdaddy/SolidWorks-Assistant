from sw_assistant import __main__


def test_installed_entrypoint_calls_application_runner(monkeypatch):
    monkeypatch.setattr(__main__, "run", lambda: 17)
    assert __main__.main() == 17


def test_pyinstaller_launcher_imports_package_entrypoint():
    import launcher

    assert launcher.main is __main__.main


def test_pyinstaller_launcher_detects_incomplete_qt_runtime(tmp_path):
    import launcher

    missing = launcher.missing_frozen_runtime_files(tmp_path)

    assert "_internal/PySide6/QtCore.pyd" in missing
    assert "_internal/PySide6/Qt6Core.dll" in missing

    for relative in launcher.REQUIRED_FROZEN_RUNTIME_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"runtime")

    assert launcher.missing_frozen_runtime_files(tmp_path) == []


def test_pyinstaller_launcher_detects_incompatible_icu_shadow(tmp_path):
    import launcher

    assert launcher.incompatible_frozen_runtime_files(tmp_path) == []
    conflicting = tmp_path / "_internal" / "icuuc.dll"
    conflicting.parent.mkdir(parents=True)
    conflicting.touch()

    assert launcher.incompatible_frozen_runtime_files(tmp_path) == [
        "_internal/icuuc.dll"
    ]
