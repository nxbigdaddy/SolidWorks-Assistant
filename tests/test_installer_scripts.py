from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_forwards_all_behavior_switches_through_elevation():
    text = (ROOT / "installer" / "Install-SolidWorksAssistant.ps1").read_text("utf-8")
    for switch in (
        "-SkipAddinRegistration",
        "-NoShortcuts",
        "-NoUninstallRegistry",
        "-Quiet",
        "-InstallRoot",
    ):
        assert f'$arguments += " {switch}' in text


def test_uninstaller_supports_valid_custom_root_and_forwards_addin_switch():
    text = (ROOT / "installer" / "Uninstall-SolidWorksAssistant.ps1").read_text("utf-8")
    assert '[IO.Path]::GetFileName($InstallRoot) -ne $ProductName' not in text
    assert 'Join-Path $InstallRoot "SolidWorksAssistant.exe"' in text
    assert 'Join-Path $InstallRoot "INSTALL_VERSION.txt"' in text
    assert '$arguments += " -SkipAddinUnregistration"' in text
    assert "foreach ($attempt in 1..30)" in text
    assert "Installation directory remained after bounded cleanup" in text


def test_build_and_installer_require_complete_qt_runtime():
    scripts = (
        ROOT / "build.ps1",
        ROOT / "installer" / "build_installer.ps1",
        ROOT / "installer" / "Install-SolidWorksAssistant.ps1",
    )

    for script in scripts:
        text = script.read_text("utf-8")
        assert "_internal\\PySide6\\QtCore.pyd" in text
        assert "_internal\\PySide6\\Qt6Core.dll" in text
        assert "_internal\\PySide6\\plugins\\platforms\\qwindows.dll" in text
        assert "_internal\\sw_assistant\\agent\\pi_assets\\provider.mjs" in text
        assert "_internal\\icuuc.dll" in text


def test_pyinstaller_preserves_agent_metadata_and_rejects_foreign_icu():
    text = (ROOT / "SolidWorksAssistant.spec").read_text("utf-8")

    assert 'copy_metadata("genai_prices")' in text
    assert 'copy_metadata("pydantic_ai_slim")' in text
    assert "pi_assets/provider.mjs" in text
    assert 'Path(binary[0]).name.casefold() != "icuuc.dll"' in text


def test_application_build_retries_intermediate_executable_cleanup():
    text = (ROOT / "build.ps1").read_text("utf-8")

    assert "foreach ($attempt in 1..30)" in text
    assert "Start-Sleep -Seconds 1" in text
    assert "Refusing to remove an intermediate outside the workspace" in text
    assert "intermediate executable could not be removed" in text
