param(
    [switch]$SkipTests,
    [switch]$SkipApplicationBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = "5.1.1"
$productId = "SolidWorksAIAssistantV5"
$release = Join-Path $root "dist\SolidWorksAssistant"
$output = Join-Path $root "artifacts\release\$version"
$portable = Join-Path $output "SolidWorksAssistant-$version-portable.zip"
$setup = Join-Path $output "SolidWorksAssistant-$version-Setup.exe"
$iexpressStage = Join-Path $root "artifacts\installer-stage\$version"

function Assert-UnderWorkspace([string]$Path) {
    $workspace = [IO.Path]::GetFullPath($root).TrimEnd("\") + "\"
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($workspace, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the workspace: $resolved"
    }
}

Set-Location $root

if (-not $SkipApplicationBuild) {
    if ($SkipTests) {
        & (Join-Path $root "build.ps1") -SkipTests
    }
    else {
        & (Join-Path $root "build.ps1")
    }
    if ($LASTEXITCODE -ne 0) { throw "Python application build failed." }

    & (Join-Path $root "scripts\build_event_bridge.ps1")
    if ($LASTEXITCODE -ne 0) { throw "SolidWorks Event Bridge build failed." }
}

if (-not (Test-Path -LiteralPath (Join-Path $release "SolidWorksAssistant.exe") -PathType Leaf)) {
    throw "Compiled application was not found: $release"
}
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
    throw "Release Qt/Python runtime is incomplete: $($missingRuntimeFiles -join ', ')"
}
$incompatibleIcu = Join-Path $release "_internal\icuuc.dll"
if (Test-Path -LiteralPath $incompatibleIcu -PathType Leaf) {
    throw "Release Qt runtime contains an incompatible ICU shadow DLL: $incompatibleIcu"
}

$eventBridgeSource = Join-Path $root "artifacts\dotnet\EventBridge\Release"
$eventBridgeDestination = Join-Path $release "EventBridge"
Assert-UnderWorkspace $eventBridgeDestination
if (Test-Path -LiteralPath $eventBridgeDestination) {
    Remove-Item -LiteralPath $eventBridgeDestination -Recurse -Force
}
New-Item -ItemType Directory -Path $eventBridgeDestination -Force | Out-Null
Get-ChildItem -LiteralPath $eventBridgeSource -File -Filter *.dll | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $eventBridgeDestination -Force
}

$forbidden = Get-ChildItem -LiteralPath $release -Recurse -File | Where-Object {
    $_.Extension -in ".db", ".jsonl", ".sldprt", ".sldasm", ".slddrw", ".pdb" -or
    $_.Name -match "(?i)(api[_-]?key|secret|credential)"
}
if ($forbidden) {
    throw "Release contains forbidden data: $($forbidden.FullName -join ', ')"
}

New-Item -ItemType Directory -Path $output -Force | Out-Null
if (Test-Path -LiteralPath $portable) { Remove-Item -LiteralPath $portable -Force }
Compress-Archive -Path (Join-Path $release "*") -DestinationPath $portable -CompressionLevel Optimal
$portableHash = (Get-FileHash -LiteralPath $portable -Algorithm SHA256).Hash

Assert-UnderWorkspace $iexpressStage
if (Test-Path -LiteralPath $iexpressStage) {
    Remove-Item -LiteralPath $iexpressStage -Recurse -Force
}
New-Item -ItemType Directory -Path $iexpressStage -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "setup.cmd") -Destination $iexpressStage
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Install-SolidWorksAssistant.ps1") -Destination $iexpressStage
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Uninstall-SolidWorksAssistant.ps1") -Destination $iexpressStage
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Uninstall-SolidWorksAssistant.cmd") -Destination $iexpressStage
Copy-Item -LiteralPath $portable -Destination $iexpressStage
Set-Content -LiteralPath (Join-Path $iexpressStage "SolidWorksAssistant-$version-portable.zip.sha256") -Value $portableHash -Encoding ASCII

$sed = Join-Path $iexpressStage "SolidWorksAssistant-$version.sed"
$stageWithSlash = [IO.Path]::GetFullPath($iexpressStage).TrimEnd("\") + "\"
$setupFull = [IO.Path]::GetFullPath($setup)
$sedText = @"
[Version]
Class=IEXPRESS
SEDVersion=3

[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=
FinishMessage=
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles

[Strings]
InstallPrompt=Install SolidWorks AI Assistant $version?
TargetName=$setupFull
FriendlyName=SolidWorks AI Assistant $version Setup
AppLaunched=setup.cmd
FILE0=setup.cmd
FILE1=Install-SolidWorksAssistant.ps1
FILE2=Uninstall-SolidWorksAssistant.ps1
FILE3=Uninstall-SolidWorksAssistant.cmd
FILE4=SolidWorksAssistant-$version-portable.zip
FILE5=SolidWorksAssistant-$version-portable.zip.sha256

[SourceFiles]
SourceFiles0=$stageWithSlash

[SourceFiles0]
%FILE0%=
%FILE1%=
%FILE2%=
%FILE3%=
%FILE4%=
%FILE5%=
"@
Set-Content -LiteralPath $sed -Value $sedText -Encoding ASCII
if (Test-Path -LiteralPath $setup) { Remove-Item -LiteralPath $setup -Force }
$iexpress = Join-Path $env:WINDIR "System32\iexpress.exe"
$process = Start-Process -FilePath $iexpress -ArgumentList @("/N", $sed) -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "IExpress installer build failed with exit code $($process.ExitCode)."
}

$setupHash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash
$checksums = @(
    "$setupHash *$([IO.Path]::GetFileName($setup))",
    "$portableHash *$([IO.Path]::GetFileName($portable))"
)
Set-Content -LiteralPath (Join-Path $output "SHA256SUMS.txt") -Value $checksums -Encoding ASCII
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "INSTALL-README.txt") -Destination $output -Force

$releaseFiles = @(Get-ChildItem -LiteralPath $release -Recurse -File)
$manifest = [ordered]@{
    product = "SolidWorks AI Assistant"
    version = $version
    built_at = [DateTimeOffset]::Now.ToString("o")
    setup = [ordered]@{
        file = [IO.Path]::GetFileName($setup)
        bytes = (Get-Item -LiteralPath $setup).Length
        sha256 = $setupHash
        authenticode = (Get-AuthenticodeSignature -LiteralPath $setup).Status.ToString()
    }
    portable = [ordered]@{
        file = [IO.Path]::GetFileName($portable)
        bytes = (Get-Item -LiteralPath $portable).Length
        sha256 = $portableHash
    }
    release_file_count = $releaseFiles.Count
    release_bytes = ($releaseFiles | Measure-Object Length -Sum).Sum
    includes_event_bridge = Test-Path -LiteralPath (Join-Path $eventBridgeDestination "SolidWorksAssistant.EventBridge.dll")
    forbidden_file_count = @($forbidden).Count
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output "release-manifest.json") -Encoding UTF8

Write-Host "Installer ready: $setup"
Write-Host "Portable package ready: $portable"
Write-Host "Setup SHA256: $setupHash"
