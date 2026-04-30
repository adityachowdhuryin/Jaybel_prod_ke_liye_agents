<#
.SYNOPSIS
  Deploy a Vertex AI Agent Engine app from vertex_agents/ using the project venv ADK CLI.

.PARAMETER Agent
  cost          -> vertex_agents/cost_metrics_agent
  orchestrator  -> vertex_agents/pa_orchestrator_agent

.PARAMETER DatabaseUrl
  Optional Postgres DSN for fallback path only. BigQuery is primary in current setup.

.PARAMETER CostDataSource
  For cost deploy: auto | bigquery | postgres (default bigquery).

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
    [string]$CostDataSource = 'bigquery',
    [string]$BillingSchemaMode = 'clean_view',
    [string]$BillingTable = 'clean_billing_view',
    [string]$BillingDataset = 'gcp_billing_data',
    [string]$BillingProject = '',
    [string]$BillingDefaultTillNowScope = 'full_history',
    [string]$BillingFullHistoryStartDate = '2026-01-01',
    [string]$AgentEngineId = '',
    [string]$CostAgentEngineId = '5616525846761177088',
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
        $billingProjectResolved = $BillingProject
        if (-not $billingProjectResolved) { $billingProjectResolved = $Project }
        $lines = @(
            "GOOGLE_CLOUD_PROJECT=$Project"
            "GOOGLE_CLOUD_LOCATION=$Region"
            "COST_DATA_SOURCE=$CostDataSource"
            "BQ_BILLING_PROJECT=$billingProjectResolved"
            "BQ_BILLING_DATASET=$BillingDataset"
            "BQ_BILLING_TABLE=$BillingTable"
            "BILLING_BQ_SCHEMA_MODE=$BillingSchemaMode"
            "BILLING_DEFAULT_TILL_NOW_SCOPE=$BillingDefaultTillNowScope"
            "BILLING_FULL_HISTORY_START_DATE=$BillingFullHistoryStartDate"
            "OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental"
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=EVENT_ONLY"
        )
        if ($db) {
            $lines += "DATABASE_URL=$db"
        }
        Set-Content -Path $CostEnv -Value $lines
        Write-Host ('Wrote {0} (COST_DATA_SOURCE={1}, BQ table={2}.{3}.{4})' -f $CostEnv, $CostDataSource, $billingProjectResolved, $BillingDataset, $BillingTable)
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

    Write-Host ""
    Write-Host "Deploy finished."
    Write-Host "If you used -ForceNewEngine, copy the new reasoning engine identity from output/console and update:"
    Write-Host "  - ORCHESTRATOR_AGENT_ENGINE_RESOURCE (local FastAPI proxy)"
    Write-Host "  - COST_AGENT_ENGINE_RESOURCE or COST_AGENT_QUERY_ENDPOINT (orchestrator agent env)"
} finally {
    Pop-Location
}
