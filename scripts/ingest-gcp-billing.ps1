<#
.SYNOPSIS
  Ingests GCP Billing Export (BigQuery) into PostgreSQL cloud_costs table.

.REQUIREMENTS
  - DATABASE_URL environment variable set.
  - ADC auth ready: gcloud auth application-default login
  - Billing export table exists in BigQuery.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$BqProject,

    [Parameter(Mandatory = $true)]
    [string]$BqDataset,

    [Parameter(Mandatory = $true)]
    [string]$BqTable,

    [Parameter(Mandatory = $true)]
    [string]$StartDate,

    [Parameter(Mandatory = $true)]
    [string]$EndDate,

    [ValidateSet('prod', 'dev')]
    [string]$DefaultEnvironment = 'prod'
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not $env:DATABASE_URL) {
    throw "DATABASE_URL is not set in current shell."
}

$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "Python venv not found at $Py"
}

& $Py ".\scripts\ingest_gcp_billing_to_postgres.py" `
    --bq-project $BqProject `
    --bq-dataset $BqDataset `
    --bq-table $BqTable `
    --start-date $StartDate `
    --end-date $EndDate `
    --default-environment $DefaultEnvironment
