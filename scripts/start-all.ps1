# Start Postgres (Docker) + Orchestrator + Next.js on the host (fast reload).
# From repo root you can instead run: docker compose up --build
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Import-GcpEnvFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    Write-Host ">>> Loaded $Path"
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#')) { return }
        if ($line.StartsWith('export ')) { $line = $line.Substring(7).Trim() }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { return }
        $k = $line.Substring(0, $eq).Trim()
        $v = $line.Substring($eq + 1).Trim().TrimEnd("`r")
        if (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        if ($k) { Set-Item -Path "Env:$k" -Value $v }
    }
}
Import-GcpEnvFile (Join-Path $RepoRoot "config\gcp.env")

New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "logs") | Out-Null

Write-Host ">>> Starting postgres (docker compose)..."
docker compose up -d postgres

$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    docker compose exec -T postgres pg_isready -U postgres 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw "Postgres did not become ready in time." }

$tbl = (docker compose exec -T postgres psql -U postgres -d postgres -tAc "SELECT to_regclass('public.cloud_costs');" 2>$null).Trim()
if ([string]::IsNullOrWhiteSpace($tbl)) {
    Write-Host ">>> Applying database/schema.sql..."
    Get-Content (Join-Path $RepoRoot "database\schema.sql") -Raw | docker compose exec -T postgres psql -U postgres -d postgres
}

$orchDir = Join-Path $RepoRoot "agents\orchestrator"
$feDir = Join-Path $RepoRoot "frontend"

$env:DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5435/postgres"

# BigQuery defaults (config\gcp.env loaded above when present)
if (-not $env:GOOGLE_CLOUD_PROJECT) { $env:GOOGLE_CLOUD_PROJECT = "gls-training-486405" }
if (-not $env:BQ_BILLING_PROJECT) { $env:BQ_BILLING_PROJECT = $env:GOOGLE_CLOUD_PROJECT }
if (-not $env:BQ_BILLING_DATASET) { $env:BQ_BILLING_DATASET = "gcp_billing_data" }
if (-not $env:BQ_BILLING_TABLE) { $env:BQ_BILLING_TABLE = "jaybel_prod_billing_view" }
if (-not $env:BQ_WORKFLOW_PROJECT) { $env:BQ_WORKFLOW_PROJECT = $env:BQ_BILLING_PROJECT }
if (-not $env:BQ_WORKFLOW_DATASET) { $env:BQ_WORKFLOW_DATASET = $env:BQ_BILLING_DATASET }
if (-not $env:BQ_WORKFLOW_TABLE) { $env:BQ_WORKFLOW_TABLE = "jaybel_prod_workflow_view" }
if (-not $env:ORCHESTRATOR_AUTH_DISABLED) { $env:ORCHESTRATOR_AUTH_DISABLED = "1" }
$saKey = Join-Path $RepoRoot "frontend\.secrets\speech-sa.json"
if (-not $env:GOOGLE_APPLICATION_CREDENTIALS -and (Test-Path $saKey)) {
    $env:GOOGLE_APPLICATION_CREDENTIALS = $saKey
}

Write-Host ">>> Starting orchestrator :8000 (logs\orchestrator.log)..."
Start-Process -FilePath "python" -ArgumentList @(
    "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $orchDir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $RepoRoot "logs\orchestrator.log") `
    -RedirectStandardError (Join-Path $RepoRoot "logs\orchestrator.err.log")

Start-Sleep -Seconds 2

if (-not (Test-Path (Join-Path $feDir "node_modules"))) {
    Write-Host ">>> npm ci (frontend)..."
    Push-Location $feDir
    npm ci
    Pop-Location
}

Write-Host ">>> Starting frontend :3000 (logs\frontend.log)..."
$feLog = Join-Path $RepoRoot "logs\frontend.log"
$feErr = Join-Path $RepoRoot "logs\frontend.err.log"
# npm is a .cmd shim on Windows; invoke via cmd.exe
Start-Process -FilePath "cmd.exe" -ArgumentList @(
    "/c", "npm run dev -- --hostname 127.0.0.1 --port 3000"
) -WorkingDirectory $feDir -WindowStyle Hidden `
    -RedirectStandardOutput $feLog -RedirectStandardError $feErr

Write-Host ""
Write-Host "Done. Open http://127.0.0.1:3000"
Write-Host "  Orchestrator   http://127.0.0.1:8000/health"
Write-Host "  Logs: .\logs\"
Write-Host "Stop host processes: .\scripts\stop-all.ps1"
Write-Host "Stop docker postgres: docker compose down"
