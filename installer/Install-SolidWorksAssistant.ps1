[CmdletBinding()]
param(
    [switch]$Elevated,
    [switch]$SkipElevation,
    [switch]$SkipAddinRegistration = $true,
    [switch]$NoShortcuts,
    [switch]$NoUninstallRegistry,
    [switch]$Quiet,
    [string]$InstallRoot = ""
)

$ErrorActionPreference = "Stop"
$ProductName = "SolidWorks AI Assistant 5.0"
$ProductVersion = "5.1.1"
$ProductId = "SolidWorksAIAssistantV5"
$ArchiveName = "SolidWorksAssistant-$ProductVersion-portable.zip"
$HashName = "$ArchiveName.sha256"

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
        if ($InstallRoot.Contains('"')) {
            throw "InstallRoot contains an invalid quote character."
        }
        $powerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated"
        if ($SkipAddinRegistration) { $arguments += " -SkipAddinRegistration" }
        if ($NoShortcuts) { $arguments += " -NoShortcuts" }
        if ($NoUninstallRegistry) { $arguments += " -NoUninstallRegistry" }
        if ($Quiet) { $arguments += " -Quiet" }
        if (-not [string]::IsNullOrWhiteSpace($InstallRoot)) {
            $arguments += " -InstallRoot `"$InstallRoot`""
        }
        $process = Start-Process -FilePath $powerShell -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $process.ExitCode
    }
    catch {
        Show-Message "Administrator approval was cancelled. Installation did not change this computer." $true
        exit 1
    }
}

$archive = Join-Path $PSScriptRoot $ArchiveName
$hashFile = Join-Path $PSScriptRoot $HashName
$staging = ""
$backup = ""
$installed = $false

try {
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        throw "Installer payload is missing: $ArchiveName"
    }
    if (-not (Test-Path -LiteralPath $hashFile -PathType Leaf)) {
        throw "Installer checksum is missing: $HashName"
    }
    $expectedHash = ((Get-Content -LiteralPath $hashFile -Raw).Trim() -split "\s+")[0]
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($expectedHash -ne $actualHash) {
        throw "Installer payload checksum verification failed."
    }

    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        $InstallRoot = Join-Path ([Environment]::GetFolderPath("ProgramFiles")) $ProductName
    }
    if ($InstallRoot.Contains('"')) {
        throw "InstallRoot contains an invalid quote character."
    }
    $InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd("\")
    if ([string]::IsNullOrWhiteSpace([IO.Path]::GetFileName($InstallRoot)) -or $InstallRoot.Length -lt 12) {
        throw "Unsafe installation directory: $InstallRoot"
    }
    $parent = Split-Path -Parent $InstallRoot
    New-Item -ItemType Directory -Path $parent -Force | Out-Null

    if ((Test-Path -LiteralPath $InstallRoot) -and (Get-Process SLDWORKS -ErrorAction SilentlyContinue)) {
        throw "Close SolidWorks before updating an existing installation."
    }
    if (Get-Process SolidWorksAssistant -ErrorAction SilentlyContinue) {
        throw "Close SolidWorks AI Assistant before installing this version."
    }

    $token = [Guid]::NewGuid().ToString("N")
    $staging = Join-Path $parent ("." + $ProductId + ".install." + $token)
    $backup = Join-Path $parent ("." + $ProductId + ".backup." + $token)
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
    if (-not (Test-Path -LiteralPath (Join-Path $staging "SolidWorksAssistant.exe") -PathType Leaf)) {
        throw "The compiled application is missing from the installer payload."
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
        -not (Test-Path -LiteralPath (Join-Path $staging $_) -PathType Leaf)
    })
    if ($missingRuntimeFiles) {
        throw "Installer Qt/Python runtime is incomplete: $($missingRuntimeFiles -join ', ')"
    }
    $incompatibleIcu = Join-Path $staging "_internal\icuuc.dll"
    if (Test-Path -LiteralPath $incompatibleIcu -PathType Leaf) {
        throw "Installer Qt runtime contains an incompatible ICU shadow DLL: $incompatibleIcu"
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Uninstall-SolidWorksAssistant.ps1") -Destination $staging -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Uninstall-SolidWorksAssistant.cmd") -Destination $staging -Force
    Set-Content -LiteralPath (Join-Path $staging "INSTALL_VERSION.txt") -Value $ProductVersion -Encoding ASCII

    if (Test-Path -LiteralPath $InstallRoot) {
        Move-Item -LiteralPath $InstallRoot -Destination $backup
    }
    Move-Item -LiteralPath $staging -Destination $InstallRoot
    $staging = ""
    $installed = $true

    $application = Join-Path $InstallRoot "SolidWorksAssistant.exe"
    $addin = Join-Path $InstallRoot "EventBridge\SolidWorksAssistant.EventBridge.dll"
    $regasm = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\RegAsm.exe"
    if (-not $SkipAddinRegistration) {
        if (-not (Test-Path -LiteralPath $regasm -PathType Leaf)) {
            throw "64-bit .NET Framework RegAsm is not installed."
        }
        if (-not (Test-Path -LiteralPath $addin -PathType Leaf)) {
            throw "SolidWorks Event Bridge is missing from the installed application."
        }
        & $regasm $addin /codebase | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "SolidWorks Event Bridge registration failed with exit code $LASTEXITCODE."
        }
    }

    if (-not $NoShortcuts) {
        $shell = New-Object -ComObject WScript.Shell
        $startFolder = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\SolidWorks AI Assistant 5.0"
        New-Item -ItemType Directory -Path $startFolder -Force | Out-Null
        $shortcut = $shell.CreateShortcut((Join-Path $startFolder "SolidWorks AI Assistant 5.0.lnk"))
        $shortcut.TargetPath = $application
        $shortcut.WorkingDirectory = $InstallRoot
        $shortcut.IconLocation = "$application,0"
        $shortcut.Save()
        $uninstallShortcut = $shell.CreateShortcut((Join-Path $startFolder "Uninstall SolidWorks AI Assistant.lnk"))
        $uninstallShortcut.TargetPath = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
        $uninstallShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallRoot 'Uninstall-SolidWorksAssistant.ps1')`""
        $uninstallShortcut.WorkingDirectory = $InstallRoot
        $uninstallShortcut.Save()
        $desktopShortcut = $shell.CreateShortcut((Join-Path $env:Public "Desktop\SolidWorks AI Assistant 5.0.lnk"))
        $desktopShortcut.TargetPath = $application
        $desktopShortcut.WorkingDirectory = $InstallRoot
        $desktopShortcut.IconLocation = "$application,0"
        $desktopShortcut.Save()
    }

    if (-not $NoUninstallRegistry) {
        $uninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ProductId"
        New-Item -Path $uninstallKey -Force | Out-Null
        $estimatedSize = [int]((Get-ChildItem -LiteralPath $InstallRoot -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
        New-ItemProperty -Path $uninstallKey -Name DisplayName -Value $ProductName -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name DisplayVersion -Value $ProductVersion -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name Publisher -Value "SolidWorks Assistant" -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name InstallLocation -Value $InstallRoot -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name DisplayIcon -Value "$application,0" -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name UninstallString -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallRoot 'Uninstall-SolidWorksAssistant.ps1')`"" -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name QuietUninstallString -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallRoot 'Uninstall-SolidWorksAssistant.ps1')`" -Quiet" -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name EstimatedSize -Value $estimatedSize -PropertyType DWord -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name NoModify -Value 1 -PropertyType DWord -Force | Out-Null
        New-ItemProperty -Path $uninstallKey -Name NoRepair -Value 1 -PropertyType DWord -Force | Out-Null
    }

    if ($backup -and (Test-Path -LiteralPath $backup)) {
        Remove-Item -LiteralPath $backup -Recurse -Force
    }
    Show-Message "Installation completed. Restart SolidWorks to load the Event Bridge, then start SolidWorks AI Assistant from the Start menu."
    Write-Host "Installed $ProductName $ProductVersion to $InstallRoot"
    exit 0
}
catch {
    $message = $_.Exception.Message
    if ($installed -and $InstallRoot -and (Test-Path -LiteralPath $InstallRoot) -and $backup -and (Test-Path -LiteralPath $backup)) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $backup -Destination $InstallRoot -ErrorAction SilentlyContinue
    }
    if ($staging -and (Test-Path -LiteralPath $staging)) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    Show-Message "Installation failed: $message" $true
    Write-Error $message
    exit 1
}
