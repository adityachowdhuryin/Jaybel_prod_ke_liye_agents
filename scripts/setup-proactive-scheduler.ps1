<#
.SYNOPSIS
  Creates or updates a Cloud Scheduler job for /proactive/morning-brief.

.DESCRIPTION
  Requires gcloud auth and Scheduler API enabled.
  Uses an API key header expected by agents/orchestrator/main.py (X-API-Key).
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Project,

    [string]$Region = "us-central1",
    [string]$JobName = "pa-morning-brief",
    [string]$Schedule = "0 8 * * *",

    [Parameter(Mandatory = $true)]
    [string]$OrchestratorUrl,

    [Parameter(Mandatory = $true)]
    [string]$ApiKey
)

$ErrorActionPreference = "Stop"

$endpoint = $OrchestratorUrl.TrimEnd("/") + "/proactive/morning-brief"
$headers = "X-API-Key=$ApiKey"

Write-Host "Configuring Cloud Scheduler job: $JobName"

$exists = $false
try {
    gcloud scheduler jobs describe $JobName `
        --project $Project `
        --location $Region | Out-Null
    $exists = $true
} catch {
    $exists = $false
}

if ($exists) {
    gcloud scheduler jobs update http $JobName `
        --project $Project `
        --location $Region `
        --schedule $Schedule `
        --uri $endpoint `
        --http-method POST `
        --headers $headers `
        --time-zone "Asia/Kolkata" | Out-Null
    Write-Host "Updated: $JobName"
} else {
    gcloud scheduler jobs create http $JobName `
        --project $Project `
        --location $Region `
        --schedule $Schedule `
        --uri $endpoint `
        --http-method POST `
        --headers $headers `
        --time-zone "Asia/Kolkata" | Out-Null
    Write-Host "Created: $JobName"
}

Write-Host "Run once now:"
Write-Host "  gcloud scheduler jobs run $JobName --project $Project --location $Region"
