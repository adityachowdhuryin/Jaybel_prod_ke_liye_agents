$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$envFile = Join-Path $Root "config/gcp.env"
if (Test-Path $envFile) {
  Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*export\s+([^=]+)=(.*)$') {
      $name = $matches[1].Trim()
      $val = $matches[2].Trim().Trim('"')
      Set-Item -Path "Env:$name" -Value $val
    }
  }
}

$py = Join-Path $Root ".venv/Scripts/python.exe"
if (-not (Test-Path $py)) {
  Write-Error "Create .venv and pip install -r requirements-adk.txt first."
}

& $py (Join-Path $Root "scripts/sync-online-monitor-to-firestore.py") @args
