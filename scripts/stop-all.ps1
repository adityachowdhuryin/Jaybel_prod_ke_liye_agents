# Stop local uvicorn + npm dev started by start-all.ps1 (best-effort by port).
$ErrorActionPreference = "SilentlyContinue"
foreach ($port in 3000, 8000, 8001) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }
}
Write-Host "Stopped listeners on ports 3000, 8000, 8001 (if any)."
