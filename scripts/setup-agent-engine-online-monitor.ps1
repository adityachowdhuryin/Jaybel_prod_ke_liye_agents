param(
    [string]$Project = "",
    [string]$Location = "",
    [string]$Resource = "",
    [string]$DisplayName = "",
    [int]$SamplingRate = 0,
    [int]$MaxSamplesPerRun = 0
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

if (Test-Path "config/gcp.env") {
    $lines = Get-Content "config/gcp.env"
    foreach ($line in $lines) {
        if ($line -match '^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)\s*$') {
            $name = $matches[1]
            $value = $matches[2].Trim('"')
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

if (-not $Project) { $Project = $env:GOOGLE_CLOUD_PROJECT }
if (-not $Location) { $Location = if ($env:GOOGLE_CLOUD_LOCATION) { $env:GOOGLE_CLOUD_LOCATION } else { "us-central1" } }
if (-not $Resource) { $Resource = $env:COST_AGENT_ENGINE_RESOURCE }
if (-not $DisplayName) { $DisplayName = if ($env:ONLINE_MONITOR_DISPLAY_NAME) { $env:ONLINE_MONITOR_DISPLAY_NAME } else { "cost-agent-online-monitor" } }
if ($SamplingRate -le 0) {
    if ($env:ONLINE_MONITOR_SAMPLING_RATE) { $SamplingRate = [int]$env:ONLINE_MONITOR_SAMPLING_RATE } else { $SamplingRate = 50 }
}
if ($MaxSamplesPerRun -le 0) {
    if ($env:ONLINE_MONITOR_MAX_SAMPLES_PER_RUN) { $MaxSamplesPerRun = [int]$env:ONLINE_MONITOR_MAX_SAMPLES_PER_RUN } else { $MaxSamplesPerRun = 200 }
}

if (-not $Project) { throw "Set GOOGLE_CLOUD_PROJECT (e.g. in config/gcp.env)." }
if (-not $Resource) { throw "Set COST_AGENT_ENGINE_RESOURCE in config/gcp.env." }

Write-Host "Configuring online monitor for cost agent..."
Write-Host "  project: $Project"
Write-Host "  location: $Location"
Write-Host "  resource: $Resource"
Write-Host "  display_name: $DisplayName"
Write-Host "  sampling_rate: $SamplingRate%"

& ".\.venv\Scripts\python.exe" "scripts/setup-agent-engine-online-monitor.py" `
  --project $Project `
  --location $Location `
  --resource $Resource `
  --display-name $DisplayName `
  --sampling-rate $SamplingRate `
  --max-evaluated-samples-per-run $MaxSamplesPerRun `
  --metrics HALLUCINATION FINAL_RESPONSE_QUALITY TOOL_USE_QUALITY SAFETY
