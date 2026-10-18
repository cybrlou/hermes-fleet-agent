# Cybrtch Hermes fleet sidecar — Windows
# Lives outside the Hermes install dir so `hermes update` does not stop it.
# Asks for hub address + token. Does not print the token.
param(
    [string]$HubUrl = $env:HUB_URL,
    [string]$AgentId = $env:AGENT_ID,
    [string]$Site = $env:SITE,
    [string]$FleetToken = $env:FLEET_TOKEN
)
$ErrorActionPreference = "Stop"

function Read-Required([string]$label, [string]$current, [switch]$Secret) {
    if ($current) { return $current }
    $v = Read-Host $label
    if (-not $v) { throw "Missing: $label" }
    return $v
}

$HubUrl = Read-Required "Hub address (IP or URL, no token)" $HubUrl
$AgentId = Read-Required "Agent id (e.g. fig-front)" $AgentId
$Site = Read-Required "Site name (e.g. fig)" $Site
$FleetToken = Read-Required "Fleet token (will not be printed)" $FleetToken -Secret

$HubUrl = $HubUrl.Trim().TrimEnd("/")
if ($HubUrl -notmatch "^https?://") {
    if ($HubUrl -match ":\d+$") { $HubUrl = "http://$HubUrl" }
    else { $HubUrl = "http://$HubUrl`:56575" }
}

$InstallDir = Join-Path $env:LOCALAPPDATA "hermes-fleet-agent"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$src = Join-Path $PSScriptRoot "src\agent.py"
if (-not (Test-Path $src)) { $src = Join-Path $PSScriptRoot "agent.py" }
if (-not (Test-Path $src)) { throw "agent.py not found next to installer" }
Copy-Item $src (Join-Path $InstallDir "agent.py") -Force

$envPath = Join-Path $InstallDir "agent.env"
@(
    "HUB_URL=$HubUrl",
    "AGENT_ID=$AgentId",
    "SITE=$Site",
    "FLEET_TOKEN=$FleetToken",
    "CHAT_TIMEOUT=180"
) | Set-Content -Path $envPath -Encoding ascii
icacls $envPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "python not on PATH" }
& $py -m pip install --user websockets | Out-Null

$action = New-ScheduledTaskAction -Execute $py -Argument "`"$InstallDir\agent.py`"" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "HermesFleetAgent" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName HermesFleetAgent
Write-Output "Sidecar installed in $InstallDir (outside Hermes). Task HermesFleetAgent started. Token not shown."
Write-Output "Survives hermes update. If hub health fails from this PC, the public IP is not on the allow list."
