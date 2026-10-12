param(
    [int]$MaxAgeMinutes = 30
)

$ErrorActionPreference = "Stop"
$bridgeRoot = Join-Path $env:LOCALAPPDATA "SolidWorksAIAssistant\event-bridge"
$eventsPath = Join-Path $bridgeRoot "events.jsonl"
$capabilitiesPath = Join-Path $bridgeRoot "capabilities.json"
$diagnosticsPath = Join-Path $bridgeRoot "diagnostics.log"

if (-not (Test-Path -LiteralPath $eventsPath)) {
    throw "Event stream was not found: $eventsPath"
}
if (-not (Test-Path -LiteralPath $capabilitiesPath)) {
    throw "Capability snapshot was not found: $capabilitiesPath"
}

$events = @(
    Get-Content -LiteralPath $eventsPath -Encoding UTF8 |
        Where-Object { $_.Trim() } |
        ForEach-Object {
            try { $_ | ConvertFrom-Json } catch { $null }
        } |
        Where-Object { $null -ne $_ }
)
$connected = $events | Where-Object { $_.name -eq "bridge.connected" } | Select-Object -Last 1
if ($null -eq $connected) {
    throw "No bridge.connected event was found."
}

$connectedAt = [DateTimeOffset]::Parse($connected.timestamp_utc)
$ageMinutes = ([DateTimeOffset]::UtcNow - $connectedAt.ToUniversalTime()).TotalMinutes
if ($ageMinutes -gt $MaxAgeMinutes) {
    throw "The latest bridge.connected event is older than $MaxAgeMinutes minutes."
}

$heartbeat = $events |
    Where-Object {
        $_.name -eq "bridge.heartbeat" -and
        $_.session_id -eq $connected.session_id -and
        [long]$_.sequence -gt [long]$connected.sequence
    } |
    Select-Object -Last 1
if ($null -eq $heartbeat) {
    throw "No heartbeat was found for the latest bridge session."
}

$snapshot = Get-Content -LiteralPath $capabilitiesPath -Encoding UTF8 -Raw | ConvertFrom-Json
$capabilities = @($snapshot.capabilities)
$available = @($capabilities | Where-Object { $_.available })
if ($available.Count -ne $capabilities.Count) {
    $missing = @($capabilities | Where-Object { -not $_.available } | ForEach-Object { $_.id })
    throw "Unavailable capabilities: $($missing -join ', ')"
}

$diagnosticsAfterConnect = $false
if (Test-Path -LiteralPath $diagnosticsPath) {
    $diagnosticsAfterConnect = (Get-Item -LiteralPath $diagnosticsPath).LastWriteTimeUtc -gt $connectedAt.UtcDateTime
}
if ($diagnosticsAfterConnect) {
    throw "The diagnostics log changed after the latest bridge connection."
}

[pscustomobject]@{
    Valid = $true
    SessionId = $connected.session_id
    ProcessId = $connected.process_id
    ConnectedAtUtc = $connected.timestamp_utc
    LatestHeartbeatUtc = $heartbeat.timestamp_utc
    SolidWorksRevision = $snapshot.solidworks_revision
    InteropVersion = $snapshot.interop_version
    AvailableCapabilities = $available.Count
    TotalCapabilities = $capabilities.Count
} | ConvertTo-Json -Depth 5
