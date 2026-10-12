param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create .venv and install .[dev] first; see README.md.' }
$oldAutoload = $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD
$oldUtf8 = $env:PYTHONUTF8
try {
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
    $env:PYTHONUTF8 = '1'
    New-Item -ItemType Directory -Path '.pytest_tmp' -Force | Out-Null
    & $python -m ruff check launcher.py src scripts tests
    if ($LASTEXITCODE -ne 0) { throw 'Ruff failed.' }
    & $python -m mypy src launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'Mypy failed.' }
    & $python -m compileall -q src scripts tests launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'Compileall failed.' }
    & $python -m pytest -q --basetemp '.pytest_tmp\verify'
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
    Write-Host 'V5 source validation passed. No CAD document was changed by this command.'
}
finally {
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = $oldAutoload
    $env:PYTHONUTF8 = $oldUtf8
}
