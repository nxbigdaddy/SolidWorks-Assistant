[CmdletBinding()]
param(
    [switch]$Elevated,
    [switch]$SkipElevation,
    [switch]$SkipAddinUnregistration = $true,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ProductName = "SolidWorks AI Assistant 5.0"
$ProductId = "SolidWorksAIAssistantV5"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Show-Message([string]$Message, [bool]$IsError = $false) {
    if ($Quiet) { return }
    try {
        Add-Type -AssemblyName PresentationFramework
        $icon = if ($IsError) {
            [System.Windows.MessageBoxImage]::Error
        } else {
            [System.Windows.MessageBoxImage]::Information
        }
        [System.Windows.MessageBox]::Show(
            $Message,
            $ProductName,
            [System.Windows.MessageBoxButton]::OK,
            $icon
        ) | Out-Null
    }
    catch {
        Write-Host $Message
    }
}

if (-not $SkipElevation -and -not (Test-IsAdministrator)) {
    try {
        $powerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated"
        if ($SkipAddinUnregistration) { $arguments += " -SkipAddinUnregistration" }
        if ($Quiet) { $arguments += " -Quiet" }
        $process = Start-Process -FilePath $powerShell -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $process.ExitCode
    }
    catch {
        Show-Message "Administrator approval was cancelled. Uninstallation did not change this computer." $true
        exit 1
    }
}

try {
    $InstallRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd("\")
    $driveRoot = [IO.Path]::GetPathRoot($InstallRoot).TrimEnd("\")
    $application = Join-Path $InstallRoot "SolidWorksAssistant.exe"
    $versionMarker = Join-Path $InstallRoot "INSTALL_VERSION.txt"
    if (
        $InstallRoot.Length -lt 12 -or
        $InstallRoot.Equals($driveRoot, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $application -PathType Leaf) -or
        -not (Test-Path -LiteralPath $versionMarker -PathType Leaf)
    ) {
        throw "Unsafe installation directory: $InstallRoot"
    }
    if (Get-Process SolidWorksAssistant -ErrorAction SilentlyContinue) {
        throw "Close SolidWorks AI Assistant before uninstalling."
    }
    if (-not $SkipAddinUnregistration -and (Get-Process SLDWORKS -ErrorAction SilentlyContinue)) {
        throw "Close SolidWorks before uninstalling the Event Bridge."
    }

    $addin = Join-Path $InstallRoot "EventBridge\SolidWorksAssistant.EventBridge.dll"
    $regasm = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\RegAsm.exe"
    if (-not $SkipAddinUnregistration -and (Test-Path -LiteralPath $regasm) -and (Test-Path -LiteralPath $addin)) {
        & $regasm $addin /unregister | Out-Host
    }

    Remove-Item -LiteralPath "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ProductId" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\SolidWorks AI Assistant 5.0") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $env:Public "Desktop\SolidWorks AI Assistant 5.0.lnk") -Force -ErrorAction SilentlyContinue
    $removed = $false
    foreach ($attempt in 1..30) {
        try {
            Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction Stop
            $removed = -not (Test-Path -LiteralPath $InstallRoot)
            if ($removed) { break }
        }
        catch {
            if ($attempt -eq 30) { throw }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $removed) {
        throw "Installation directory remained after bounded cleanup: $InstallRoot"
    }
    Show-Message "SolidWorks AI Assistant was removed. Local user settings and model-library data were preserved."
    exit 0
}
catch {
    Show-Message "Uninstallation failed: $($_.Exception.Message)" $true
    Write-Error $_.Exception.Message
    exit 1
}
