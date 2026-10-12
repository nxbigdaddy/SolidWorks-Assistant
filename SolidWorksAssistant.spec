# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

hiddenimports = [
    "pythoncom",
    "pywintypes",
    "win32crypt",
    "win32com.client",
    "win32com.client.dynamic",
    "multiprocessing",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

a = Analysis(
    ["launcher.py"],
    pathex=["src"],
    binaries=[],
    datas=(
        copy_metadata("genai_prices")
        + copy_metadata("pydantic_ai_slim")
        + [("src/sw_assistant/agent/pi_assets/provider.mjs", "sw_assistant/agent/pi_assets")]
    ),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)

# Qt 6.11 on Windows links against the unversioned Windows ICU forwarder.
# PyInstaller can accidentally resolve ``icuuc.dll`` from an unrelated PATH
# entry (for example Poppler's versioned ICU runtime).  That foreign DLL has
# different exports and makes QtCore fail with Windows error 127.  Keep it out
# of the bundle so Windows resolves its own System32 forwarder.
a.binaries = [
    binary
    for binary in a.binaries
    if Path(binary[0]).name.casefold() != "icuuc.dll"
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SolidWorksAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    version="installer/version_info.txt",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SolidWorksAssistant",
)
