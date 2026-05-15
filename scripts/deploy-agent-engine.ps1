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
    [string]$BillingTable = 'jaybel_prod_billing_view',
    [string]$BillingDataset = 'gcp_billing_data',
    [string]$BillingProject = '',
    [string]$BillingDefaultTillNowScope = 'full_history',
    [string]$BillingFullHistoryStartDate = '2026-01-01',
    [string]$WorkflowTable = 'jaybel_prod_workflow_view',
    [string]$WorkflowDataset = '',
    [string]$WorkflowProject = '',
    [string]$CostEventsTable = '',
    [string]$CostEventsDataset = '',
    [string]$CostEventsProject = '',
    [string]$BillingDefaultProjectId = '',
    [string]$AgentEngineId = '',
    [string]$CostAgentEngineId = '3600096288210681856',
    [string]$OrchestratorAgentEngineId = '7920943888905273344',
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
            "BILLING_AGENT_LLM_SQL=1"
            "BILLING_CONTEXT_ROUTER_ENABLED=1"
            "BILLING_LLM_PROVIDER=auto"
            "BILLING_LLM_MAX_BYTES_BILLED=1000000000"
            "BILLING_LLM_MAX_LOOKBACK_DAYS=0"
            "OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental"
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=EVENT_ONLY"
        )
        if ($db) {
            $lines += "DATABASE_URL=$db"
        }
        if ($WorkflowTable) {
            $wp = $WorkflowProject
            if (-not $wp) { $wp = $billingProjectResolved }
            $wd = $WorkflowDataset
            if (-not $wd) { $wd = $BillingDataset }
            $lines += "BQ_WORKFLOW_PROJECT=$wp"
            $lines += "BQ_WORKFLOW_DATASET=$wd"
            $lines += "BQ_WORKFLOW_TABLE=$WorkflowTable"
        }
        if ($CostEventsTable) {
            $cep = $CostEventsProject
            if (-not $cep) { $cep = $billingProjectResolved }
            $ced = $CostEventsDataset
            if (-not $ced) { $ced = $BillingDataset }
            $lines += "BQ_COST_EVENTS_PROJECT=$cep"
            $lines += "BQ_COST_EVENTS_DATASET=$ced"
            $lines += "BQ_COST_EVENTS_TABLE=$CostEventsTable"
        }
        if ($BillingDefaultProjectId) {
            $lines += "BILLING_DEFAULT_PROJECT_ID=$BillingDefaultProjectId"
        }
        $lines += ''
        $lines += '# Optional: BILLING_SCHEMA_DIGEST=1 — live BigQuery column digest for router + SQL.'
        $lines += '# Optional legacy: BILLING_LEGACY_REGEX_ROUTING=1 — regex bq_target overrides + silent trace window shortcut.'
        $lines += '# Optional: BILLING_DETERMINISTIC_TRACE_TOTAL=1 — deterministic SUM for scalar trace totals on workflow view.'
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
