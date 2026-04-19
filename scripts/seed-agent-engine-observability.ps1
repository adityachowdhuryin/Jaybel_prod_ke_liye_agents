param(
    [string]$CostAgentResource = "",
    [string]$EvalGcsDest = ""
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

$project = $env:GOOGLE_CLOUD_PROJECT
$location = if ($env:GOOGLE_CLOUD_LOCATION) { $env:GOOGLE_CLOUD_LOCATION } else { "us-central1" }
$orchestratorResource = $env:ORCHESTRATOR_AGENT_ENGINE_RESOURCE
if (-not $CostAgentResource) { $CostAgentResource = $env:COST_AGENT_ENGINE_RESOURCE }
if (-not $EvalGcsDest) { $EvalGcsDest = $env:AGENT_ENGINE_EVAL_GCS_DEST }

if (-not $project) { throw "Set GOOGLE_CLOUD_PROJECT (e.g. in config/gcp.env)." }
if (-not $orchestratorResource) { throw "Set ORCHESTRATOR_AGENT_ENGINE_RESOURCE in config/gcp.env." }
if (-not $CostAgentResource) { throw "Set COST_AGENT_ENGINE_RESOURCE in config/gcp.env or pass -CostAgentResource." }
if (-not $EvalGcsDest) { throw "Set AGENT_ENGINE_EVAL_GCS_DEST in config/gcp.env or pass -EvalGcsDest." }

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")

& ".\.venv\Scripts\python.exe" "scripts/agent-engine-memory-smoke.py" `
  --project $project `
  --location $location `
  --resource $orchestratorResource `
  --resource $CostAgentResource `
  --scenarios "scripts/evals/memory_seed_cases.json" `
  --out "logs/agent-engine-memory-seed-report-$timestamp.json"

& ".\.venv\Scripts\python.exe" "scripts/agent-engine-create-eval.py" `
  --project $project `
  --location $location `
  --resource $orchestratorResource `
  --cases "scripts/evals/agent_engine_eval_cases.json" `
  --publish-to-vertex `
  --gcs-dest $EvalGcsDest `
  --display-name "orchestrator-eval-$timestamp" `
  --label "component=orchestrator" `
  --label "run_source=seed-agent-engine-observability" `
  --out "logs/agent-engine-eval-orchestrator-$timestamp.json"

& ".\.venv\Scripts\python.exe" "scripts/agent-engine-create-eval.py" `
  --project $project `
  --location $location `
  --resource $CostAgentResource `
  --cases "scripts/evals/agent_engine_eval_cases.json" `
  --publish-to-vertex `
  --gcs-dest $EvalGcsDest `
  --display-name "cost-agent-eval-$timestamp" `
  --label "component=cost_agent" `
  --label "run_source=seed-agent-engine-observability" `
  --out "logs/agent-engine-eval-cost-$timestamp.json"

Write-Host "Observability seeding complete."
