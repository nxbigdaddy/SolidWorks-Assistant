param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = if (Test-Path -LiteralPath ".venv\Scripts\python.exe") {
    (Resolve-Path -LiteralPath ".venv\Scripts\python.exe").Path
} else {
    (Get-Command python -ErrorAction Stop).Source
}

if (-not $SkipTests) {
    New-Item -ItemType Directory -Force -Path ".pytest_tmp" | Out-Null
    & $python -m ruff check launcher.py src scripts tests
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed" }
    & $python -m mypy src
    if ($LASTEXITCODE -ne 0) { throw "Mypy failed" }
    & $python -m compileall -q launcher.py src scripts tests
    if ($LASTEXITCODE -ne 0) { throw "Compileall failed" }
    $previousPluginAutoload = $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD
    try {
        $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
        & $python -m pytest -q --basetemp ".pytest_tmp\build"
        if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
    }
    finally {
        $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = $previousPluginAutoload
    }
}

& $python -m PyInstaller --noconfirm --clean "SolidWorksAssistant.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$release = Join-Path $PSScriptRoot "dist\SolidWorksAssistant"
$requiredRuntimeFiles = @(
    "_internal\python3.dll",
    "_internal\python312.dll",
    "_internal\shiboken6\shiboken6.abi3.dll",
    "_internal\PySide6\QtCore.pyd",
    "_internal\PySide6\Qt6Core.dll",
    "_internal\PySide6\QtGui.pyd",
    "_internal\PySide6\Qt6Gui.dll",
    "_internal\PySide6\QtWidgets.pyd",
    "_internal\PySide6\Qt6Widgets.dll",
    "_internal\PySide6\plugins\platforms\qwindows.dll",
    "_internal\sw_assistant\agent\pi_assets\provider.mjs"
)
$missingRuntimeFiles = @($requiredRuntimeFiles | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $release $_) -PathType Leaf)
})
if ($missingRuntimeFiles) {
    throw "Packaged Qt/Python runtime is incomplete: $($missingRuntimeFiles -join ', ')"
}
$incompatibleIcu = Join-Path $release "_internal\icuuc.dll"
if (Test-Path -LiteralPath $incompatibleIcu -PathType Leaf) {
    throw "Packaged Qt runtime contains an incompatible ICU shadow DLL: $incompatibleIcu"
}
$help = Join-Path $release "Help"
New-Item -ItemType Directory -Force -Path $help | Out-Null
Copy-Item -LiteralPath "README.md" -Destination $release -Force
Copy-Item -LiteralPath "docs" -Destination $help -Recurse -Force

$forbidden = Get-ChildItem -LiteralPath $release -Recurse -File | Where-Object {
    $_.Extension -in ".db", ".jsonl", ".sldprt", ".sldasm", ".slddrw"
}
if ($forbidden) {
    throw "Release contains forbidden user data: $($forbidden.FullName -join ', ')"
}

# PyInstaller leaves an EXE with the same name/hash under build/, but that is
# only an intermediate and has no adjacent _internal runtime.  Remove it after
# COLLECT succeeds so it cannot be mistaken for the distributable application.
$intermediateExecutable = Join-Path $PSScriptRoot "build\SolidWorksAssistant\SolidWorksAssistant.exe"
if (Test-Path -LiteralPath $intermediateExecutable -PathType Leaf) {
    $workspacePrefix = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd("\") + "\"
    $intermediateResolved = [IO.Path]::GetFullPath($intermediateExecutable)
    if (-not $intermediateResolved.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove an intermediate outside the workspace: $intermediateResolved"
    }
    $removed = $false
    foreach ($attempt in 1..30) {
        try {
            Remove-Item -LiteralPath $intermediateResolved -Force -ErrorAction Stop
            $removed = $true
            break
        }
        catch {
            if ($attempt -eq 30) { throw }
            Start-Sleep -Seconds 1
        }
    }
    if (-not $removed -or (Test-Path -LiteralPath $intermediateResolved)) {
        throw "PyInstaller intermediate executable could not be removed: $intermediateResolved"
    }
}

Write-Host "Release ready: $release"
