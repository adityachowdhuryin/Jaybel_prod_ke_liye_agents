<#
.SYNOPSIS
  Deploy a Vertex AI Agent Engine app from vertex_agents/ using the project venv ADK CLI.

.PARAMETER Agent
  cost          -> vertex_agents/cost_metrics_agent
  orchestrator  -> vertex_agents/pa_orchestrator_agent

.PARAMETER DatabaseUrl
  For cost deploy: Postgres DSN baked into Agent Engine env. Vertex cannot reach your laptop's
  127.0.0.1 - use a TCP tunnel (ngrok/cloudflared) host:port per docs/PHASE2-RUNBOOK.md.
  If omitted, uses $env:DATABASE_URL when set; otherwise omits DATABASE_URL (runtime uses code default).

.PARAMETER CostDataSource
  For cost deploy: auto | bigquery | postgres (default postgres = local/self-hosted Postgres, not Cloud SQL).

.PARAMETER AgentEngineId
  Numeric reasoning engine ID to update in place (keeps :query URL stable). Omit to create a new engine.

.PARAMETER CostAgentEngineId
  Cost specialist reasoning engine ID (update target + default COST_AGENT_QUERY_ENDPOINT for orchestrator).

.PARAMETER OrchestratorAgentEngineId
  Orchestrator reasoning engine ID when updating the orchestrator in place (default matches prior deploy).

.PARAMETER ForceNewEngine
  If set, do not pass --agent_engine_id (creates a new reasoning engine; update URLs afterward).
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('cost', 'orchestrator')]
    [string]$Agent,

    [string]$Project = 'gls-training-486405',
    [string]$Region = 'us-central1',
    [string]$CostAgentQueryEndpoint = '',
    [string]$DatabaseUrl = '',
    [string]$CostDataSource = 'postgres',
    [string]$AgentEngineId = '',
    [string]$CostAgentEngineId = '2939267809684750336',
    [string]$OrchestratorAgentEngineId = '8296018091465244672',
    [switch]$ForceNewEngine
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Adk = Join-Path $RepoRoot '.venv\Scripts\adk.exe'
if (-not (Test-Path $Adk)) {
    throw "ADK not found at $Adk - create .venv and: pip install -r requirements-adk.txt"
}

$AgentDir = switch ($Agent) {
    'cost' { Join-Path $RepoRoot 'vertex_agents\cost_metrics_agent' }
    'orchestrator' { Join-Path $RepoRoot 'vertex_agents\pa_orchestrator_agent' }
}

Push-Location $RepoRoot
try {
    if (-not $AgentEngineId -and -not $ForceNewEngine) {
        if ($Agent -eq 'cost') { $AgentEngineId = $CostAgentEngineId }
        elseif ($Agent -eq 'orchestrator') { $AgentEngineId = $OrchestratorAgentEngineId }
    }

    $deployArgs = @(
        'deploy', 'agent_engine',
        '--project', $Project,
        '--region', $Region,
        '--trace_to_cloud',
        '--otel_to_cloud'
    )
    if ($AgentEngineId) {
        $deployArgs += @('--agent_engine_id', $AgentEngineId)
    }

    if ($Agent -eq 'cost') {
        $db = $DatabaseUrl
        if (-not $db) { $db = $env:DATABASE_URL }
        $CostEnv = Join-Path $AgentDir '.env'
        $lines = @(
            "GOOGLE_CLOUD_PROJECT=$Project"
            "GOOGLE_CLOUD_LOCATION=$Region"
            "COST_DATA_SOURCE=$CostDataSource"
        )
        if ($db) {
            $lines += "DATABASE_URL=$db"
        }
        Set-Content -Path $CostEnv -Value $lines
        Write-Host ('Wrote {0} (COST_DATA_SOURCE={1}). Vertex must reach Postgres via a tunnel hostname (not 127.0.0.1). See docs/PHASE2-RUNBOOK.md' -f $CostEnv, $CostDataSource)
    }

    if ($Agent -eq 'orchestrator') {
        $endpoint = $CostAgentQueryEndpoint
        if (-not $endpoint) {
            $endpoint = "https://$Region-aiplatform.googleapis.com/v1/projects/$Project/locations/$Region/reasoningEngines/${CostAgentEngineId}:query"
        }
        $OrchEnv = Join-Path $AgentDir '.env'
        Set-Content -Path $OrchEnv -Value @(
            "GOOGLE_CLOUD_PROJECT=$Project"
            "GOOGLE_CLOUD_LOCATION=$Region"
            "COST_AGENT_QUERY_ENDPOINT=$endpoint"
        )
        Write-Host ('Orchestrator COST_AGENT_QUERY_ENDPOINT -> {0}' -f $endpoint)
    }

    & $Adk @deployArgs $AgentDir
} finally {
    Pop-Location
}
