param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [string]$SolidWorksApiPath = "C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\api\redist"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$solution = Join-Path $root "src-dotnet\SolidWorksAssistant.EventBridge.sln"
$msbuild = "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe"

if (-not (Test-Path -LiteralPath $msbuild)) {
    throw "MSBuild was not found: $msbuild"
}
if (-not (Test-Path -LiteralPath (Join-Path $SolidWorksApiPath "SolidWorks.Interop.sldworks.dll"))) {
    throw "SOLIDWORKS API Interop was not found: $SolidWorksApiPath"
}

& $msbuild $solution /nologo /m /t:Rebuild "/p:Configuration=$Configuration" "/p:Platform=x64" "/p:SolidWorksApiPath=$SolidWorksApiPath"
if ($LASTEXITCODE -ne 0) {
    throw "Event Bridge build failed. MSBuild exit code: $LASTEXITCODE"
}

$testExe = Join-Path $root "artifacts\dotnet\EventBridge.Tests\$Configuration\SolidWorksAssistant.EventBridge.Tests.exe"
& $testExe
if ($LASTEXITCODE -ne 0) {
    throw "Event Bridge tests failed. Exit code: $LASTEXITCODE"
}

$addinDll = Join-Path $root "artifacts\dotnet\EventBridge\$Configuration\SolidWorksAssistant.EventBridge.dll"
Write-Host "Event Bridge build and tests passed: $addinDll"
