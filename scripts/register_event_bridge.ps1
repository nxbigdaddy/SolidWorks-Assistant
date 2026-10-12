param(
    [ValidateSet("Register", "Unregister")]
    [string]$Action = "Register",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dll = Join-Path $root "artifacts\dotnet\EventBridge\$Configuration\SolidWorksAssistant.EventBridge.dll"
$regasm = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\RegAsm.exe"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)

if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "SOLIDWORKS add-in registration requires an elevated PowerShell process."
}
if (-not (Test-Path -LiteralPath $regasm)) {
    throw "64-bit RegAsm was not found: $regasm"
}
if (-not (Test-Path -LiteralPath $dll)) {
    throw "Add-in DLL was not found. Run scripts\build_event_bridge.ps1 first: $dll"
}

if ($Action -eq "Register") {
    & $regasm $dll /codebase
} else {
    & $regasm $dll /unregister
}
if ($LASTEXITCODE -ne 0) {
    throw "RegAsm failed. Exit code: $LASTEXITCODE"
}

Write-Host "$Action completed: $dll"
