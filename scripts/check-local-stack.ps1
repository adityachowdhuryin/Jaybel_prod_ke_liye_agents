# Quick health checks for locally running stack.
$ErrorActionPreference = "Stop"

function Test-Endpoint([string]$name, [string]$url) {
    try {
        $resp = Invoke-RestMethod -Method GET -Uri $url -TimeoutSec 10
        Write-Host ("[OK] {0} -> {1}" -f $name, ($resp | ConvertTo-Json -Compress))
    } catch {
        Write-Host ("[FAIL] {0} -> {1}" -f $name, $_.Exception.Message)
        $script:failed = $true
    }
}

$failed = $false
Test-Endpoint "cost-agent" "http://127.0.0.1:8001/health"
Test-Endpoint "orchestrator" "http://127.0.0.1:8000/health"

if ($failed) {
    exit 1
}
