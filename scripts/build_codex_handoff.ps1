[CmdletBinding()]
param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path (Join-Path $scriptDir "..")).Path.TrimEnd("\")
$stamp = Get-Date -Format "yyyyMMdd"
$packageBase = "SolidWorksAssistant-Codex-Handoff-$stamp"

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $root "$packageBase.zip"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $root $OutputPath
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

$excludedDirectoryNames = @(
    ".git", ".agents", ".codex", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".pytest_tmp", ".pytest-tmp", "__pycache__", "node_modules", ".venv", "venv",
    "build", "dist", "artifacts", "bin", "obj", "logs", "parts_w",
    "tasks", "task-agent", "pi-agent", "sessions", "event-bridge"
)
$excludedExtensions = @(
    ".pyc", ".pyo", ".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3",
    ".jsonl", ".log", ".exe", ".dll", ".pdb", ".chm", ".zip",
    ".sldprt", ".sldasm", ".slddrw", ".step", ".stp", ".iges", ".igs",
    ".stl", ".dwg", ".dxf", ".pdf", ".tmp", ".sha256", ".key", ".pem", ".pfx", ".p12"
)
$excludedLeafNames = @(
    ".coverage", ".env", "config.json", "config.json.bak", "desktop.ini", "thumbs.db",
    "v4-import.json", "legacy-migration-v3.json", "legacy-credential-reentry-required"
)
$configuredDataRoot = ""
if (-not [string]::IsNullOrWhiteSpace($env:SW_ASSISTANT_DATA_DIR)) {
    $configuredDataRoot = [System.IO.Path]::GetFullPath($env:SW_ASSISTANT_DATA_DIR).TrimEnd("\")
    if ($configuredDataRoot.Equals($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The project root is configured as a runtime data directory. Separate runtime data before packaging source."
    }
}
$runtimeDirectoryCache = @{}

function Test-RuntimeDataFile {
    param([Parameter(Mandatory = $true)][System.IO.FileInfo]$File)

    if ($configuredDataRoot -and $File.FullName.StartsWith(
        $configuredDataRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    # A custom runtime directory can remain in the checkout after the environment
    # override is removed. Exclude the entire directory, not only known secrets.
    $directory = $File.Directory
    while ($directory -and -not $directory.FullName.Equals(
        $root, [System.StringComparison]::OrdinalIgnoreCase)) {
        $name = $directory.FullName
        if (-not $runtimeDirectoryCache.ContainsKey($name)) {
            $runtimeDirectoryCache[$name] = $false
            foreach ($marker in @("settings.json", "library.db", "history.db", "v4-import.json")) {
                if (Test-Path -LiteralPath (Join-Path $name $marker) -PathType Leaf) {
                    $runtimeDirectoryCache[$name] = $true
                    break
                }
            }
        }
        if ($runtimeDirectoryCache[$name]) {
            return $true
        }
        $directory = $directory.Parent
    }
    return $false
}

function Get-RelativePath {
    param([Parameter(Mandatory = $true)][string]$FullName)
    return $FullName.Substring($root.Length).TrimStart("\").Replace("\", "/")
}

function Get-SourceFileHash {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    # Use the BCL directly so clean Windows PowerShell hosts do not depend on
    # optional module auto-loading to provide Get-FileHash.
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($LiteralPath)
    try {
        return [System.BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace("-", "")
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Test-IncludedFile {
    param([Parameter(Mandatory = $true)][System.IO.FileInfo]$File)

    if (Test-RuntimeDataFile -File $File) {
        return $false
    }
    $relative = Get-RelativePath -FullName $File.FullName
    $segments = $relative.Split("/")
    foreach ($segment in $segments[0..([Math]::Max(0, $segments.Length - 2))]) {
        if ($excludedDirectoryNames -contains $segment.ToLowerInvariant()) {
            return $false
        }
        if ($segment.StartsWith(".pytest-", [System.StringComparison]::OrdinalIgnoreCase) -or
            $segment.StartsWith(".test-tmp", [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
    }

    $leaf = $File.Name.ToLowerInvariant()
    if ($excludedLeafNames -contains $leaf) {
        return $false
    }
    if ($leaf -like ".env*" -or $leaf -like "settings*.json*" -or
        $leaf -like "auth*.json*" -or $leaf -like "credentials*.json*" -or
        $leaf -like "token*.json*") {
        return $false
    }
    if ($excludedExtensions -contains $File.Extension.ToLowerInvariant()) {
        return $false
    }
    if ($File.FullName -eq $OutputPath -or $File.FullName -eq "$OutputPath.sha256") {
        return $false
    }
    return $true
}

$requiredRelativePaths = @(
    "CODEX-HANDOFF.md",
    "README.md",
    "pyproject.toml",
    "src/sw_assistant/__init__.py",
    "src-dotnet/SolidWorksAssistant.EventBridge/SolidWorksAssistant.EventBridge.csproj",
    "tests/test_runtime.py",
    "docs/index.md",
    "installer/build_installer.ps1"
)

$tempRoot = [System.IO.Path]::GetFullPath((Join-Path ([System.IO.Path]::GetTempPath()) "$packageBase-$([Guid]::NewGuid().ToString('N'))"))
$systemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd("\") + "\"
if (-not $tempRoot.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use an unexpected staging directory: $tempRoot"
}
$packageRoot = Join-Path $tempRoot $packageBase

try {
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

    $sourceFiles = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force | Where-Object { Test-IncludedFile -File $_ })
    if ($sourceFiles.Count -eq 0) {
        throw "No source files were selected for the handoff package."
    }

    foreach ($file in $sourceFiles) {
        $relative = Get-RelativePath -FullName $file.FullName
        $destination = Join-Path $packageRoot $relative.Replace("/", "\")
        $destinationDirectory = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $destinationDirectory -PathType Container)) {
            New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        }
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
    }

    foreach ($required in $requiredRelativePaths) {
        $requiredPath = Join-Path $packageRoot $required.Replace("/", "\")
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Required handoff file is missing: $required"
        }
    }

    $manifestLines = foreach ($file in (Get-ChildItem -LiteralPath $packageRoot -Recurse -File | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($packageRoot.Length).TrimStart("\").Replace("\", "/")
        $hash = Get-SourceFileHash -LiteralPath $file.FullName
        "$hash *$relative"
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $manifestPath = Join-Path $packageRoot "CODEX-HANDOFF-MANIFEST.sha256"
    [System.IO.File]::WriteAllLines($manifestPath, $manifestLines, $utf8NoBom)

    if (Test-Path -LiteralPath $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }
    Add-Type -AssemblyName System.IO.Compression
    $zipStream = [System.IO.File]::Open(
        $OutputPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $archive = New-Object System.IO.Compression.ZipArchive(
            $zipStream,
            [System.IO.Compression.ZipArchiveMode]::Create,
            $false
        )
        try {
            foreach ($file in (Get-ChildItem -LiteralPath $packageRoot -Recurse -File | Sort-Object FullName)) {
                $relative = $file.FullName.Substring($packageRoot.Length).TrimStart("\").Replace("\", "/")
                $entry = $archive.CreateEntry(
                    "$packageBase/$relative",
                    [System.IO.Compression.CompressionLevel]::Optimal
                )
                $inputStream = [System.IO.File]::OpenRead($file.FullName)
                $entryStream = $entry.Open()
                try {
                    $inputStream.CopyTo($entryStream)
                } finally {
                    $entryStream.Dispose()
                    $inputStream.Dispose()
                }
            }
        } finally {
            $archive.Dispose()
        }
    } finally {
        $zipStream.Dispose()
    }

    $zipHash = Get-SourceFileHash -LiteralPath $OutputPath
    $sidecarPath = "$OutputPath.sha256"
    [System.IO.File]::WriteAllText(
        $sidecarPath,
        "$zipHash *$([System.IO.Path]::GetFileName($OutputPath))`r`n",
        $utf8NoBom
    )

    $result = Get-Item -LiteralPath $OutputPath
    [PSCustomObject]@{
        Package = $result.FullName
        Files = $sourceFiles.Count + 1
        Bytes = $result.Length
        SHA256 = $zipHash
        Sidecar = $sidecarPath
    } | Format-List
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        $resolvedTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
        if ($resolvedTempRoot.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase) -and
            $resolvedTempRoot -ne $systemTemp.TrimEnd("\")) {
            Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force
        }
    }
}
