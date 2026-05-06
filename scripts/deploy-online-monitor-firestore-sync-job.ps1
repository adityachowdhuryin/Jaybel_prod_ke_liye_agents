param(
    [string]$Project = $env:GOOGLE_CLOUD_PROJECT,
    [string]$Region = $(if ($env:ONLINE_EVAL_SYNC_RUN_REGION) { $env:ONLINE_EVAL_SYNC_RUN_REGION } elseif ($env:GOOGLE_CLOUD_LOCATION) { $env:GOOGLE_CLOUD_LOCATION } else { "us-central1" }),
    [string]$JobName = $(if ($env:ONLINE_EVAL_SYNC_JOB_NAME) { $env:ONLINE_EVAL_SYNC_JOB_NAME } else { "online-eval-firestore-sync" }),
    [string]$SchedulerJobName = $(if ($env:ONLINE_EVAL_SYNC_SCHEDULER_JOB_NAME) { $env:ONLINE_EVAL_SYNC_SCHEDULER_JOB_NAME } else { "online-eval-firestore-sync-daily" }),
    [string]$Schedule = $(if ($env:ONLINE_EVAL_SYNC_SCHEDULE) { $env:ONLINE_EVAL_SYNC_SCHEDULE } else { "0 0 * * *" }),
    [string]$TimeZone = $(if ($env:ONLINE_EVAL_SYNC_TIME_ZONE) { $env:ONLINE_EVAL_SYNC_TIME_ZONE } else { "Etc/UTC" })
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot "config\gcp.env"
if (Test-Path $EnvFile) {
    Write-Host "Load env from config/gcp.env before running this script in PowerShell."
}

if (-not $Project) {
    throw "Set GOOGLE_CLOUD_PROJECT first."
}
if (-not $env:ONLINE_EVALUATOR_RESOURCE) {
    throw "Set ONLINE_EVALUATOR_RESOURCE first."
}

$Collection = if ($env:ONLINE_EVAL_FIRESTORE_COLLECTION) { $env:ONLINE_EVAL_FIRESTORE_COLLECTION } else { "cost_agent_online_eval_traces" }
$RuntimeSa = if ($env:ONLINE_EVAL_SYNC_RUNTIME_SA) { $env:ONLINE_EVAL_SYNC_RUNTIME_SA } else { "online-eval-sync-sa@$Project.iam.gserviceaccount.com" }
$SchedulerSa = if ($env:ONLINE_EVAL_SYNC_SCHEDULER_INVOKER_SA) { $env:ONLINE_EVAL_SYNC_SCHEDULER_INVOKER_SA } else { "online-eval-sync-scheduler@$Project.iam.gserviceaccount.com" }
$ScanAgentName = if ($env:ONLINE_EVAL_SCAN_GEN_AI_AGENT_NAME) { $env:ONLINE_EVAL_SCAN_GEN_AI_AGENT_NAME } else { "" }
$IncludeAgentWithoutEvalLabels = ($env:ONLINE_EVAL_SYNC_INCLUDE_AGENT_TRACES_WITHOUT_EVAL_LABELS -eq "1")
$ArRepo = if ($env:ONLINE_EVAL_SYNC_AR_REPO) { $env:ONLINE_EVAL_SYNC_AR_REPO } else { "cloud-run-jobs" }
$Image = "us-central1-docker.pkg.dev/$Project/$ArRepo/$JobName`:latest"
$ScanMaxListTraces = if ($env:ONLINE_EVAL_SYNC_SCAN_MAX_LIST_TRACES) { $env:ONLINE_EVAL_SYNC_SCAN_MAX_LIST_TRACES } else { "3000" }
$MaxTraces = if ($env:ONLINE_EVAL_SYNC_MAX_TRACES) { $env:ONLINE_EVAL_SYNC_MAX_TRACES } else { "200" }
$PageSize = if ($env:ONLINE_EVAL_SYNC_PAGE_SIZE) { $env:ONLINE_EVAL_SYNC_PAGE_SIZE } else { "50" }
$Lookback = if ($env:ONLINE_EVAL_SYNC_LOOKBACK_MINUTES) { $env:ONLINE_EVAL_SYNC_LOOKBACK_MINUTES } else { "180" }
$Overlap = if ($env:ONLINE_EVAL_SYNC_OVERLAP_MINUTES) { $env:ONLINE_EVAL_SYNC_OVERLAP_MINUTES } else { "45" }
$TaskTimeout = if ($env:ONLINE_EVAL_SYNC_TASK_TIMEOUT) { $env:ONLINE_EVAL_SYNC_TASK_TIMEOUT } else { "1800s" }
$SkipCloudBuild = ($env:ONLINE_EVAL_SYNC_SKIP_CLOUD_BUILD -eq "1")

gcloud services enable run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com cloudtrace.googleapis.com firestore.googleapis.com --project $Project

try { gcloud artifacts repositories describe $ArRepo --location us-central1 --project $Project *> $null } catch {
    gcloud artifacts repositories create $ArRepo --repository-format docker --location us-central1 --description "Images for Cloud Run Jobs" --project $Project
}

try { gcloud iam service-accounts describe $RuntimeSa --project $Project *> $null } catch {
    gcloud iam service-accounts create ($RuntimeSa.Split("@")[0]) --display-name "Online Eval Firestore Sync Runtime" --project $Project
}
try { gcloud iam service-accounts describe $SchedulerSa --project $Project *> $null } catch {
    gcloud iam service-accounts create ($SchedulerSa.Split("@")[0]) --display-name "Online Eval Firestore Sync Scheduler Invoker" --project $Project
}

gcloud projects add-iam-policy-binding $Project --member "serviceAccount:$RuntimeSa" --role roles/cloudtrace.user *> $null
gcloud projects add-iam-policy-binding $Project --member "serviceAccount:$RuntimeSa" --role roles/datastore.user *> $null
gcloud projects add-iam-policy-binding $Project --member "serviceAccount:$RuntimeSa" --role roles/logging.logWriter *> $null

if (-not $SkipCloudBuild) {
  Push-Location $RepoRoot
  try {
    gcloud builds submit --project $Project --config infra/cloudrun/sync-online-monitor-firestore/cloudbuild.yaml --substitutions "_IMAGE=$Image" .
  } finally {
    Pop-Location
  }
}
else {
  Write-Host "Skipping Cloud Build (ONLINE_EVAL_SYNC_SKIP_CLOUD_BUILD=1); reusing image $Image."
}

if ($ScanAgentName) {
  Write-Host "Deploying job (scan mode; post-filter: evaluator OR gen_ai.agent.name=$ScanAgentName)."
} else {
  Write-Host "Deploying job (scan mode; post-filter: evaluator spans only — no gen_ai widener)."
}

$DeployEnv = @(
  "GOOGLE_CLOUD_PROJECT=$Project",
  "ONLINE_EVALUATOR_RESOURCE=$env:ONLINE_EVALUATOR_RESOURCE",
  "ONLINE_EVAL_FIRESTORE_COLLECTION=$Collection"
)
if ($ScanAgentName) {
  $DeployEnv += "ONLINE_EVAL_SCAN_GEN_AI_AGENT_NAME=$ScanAgentName"
}
$JoinedEnv = $DeployEnv -join ","

$JobArgList = @(
  "--project=$Project",
  "--online-evaluator=$env:ONLINE_EVALUATOR_RESOURCE",
  "--collection=$Collection",
  "--scan-without-list-filter",
  "--scan-max-list-traces=$ScanMaxListTraces",
  "--max-traces=$MaxTraces",
  "--page-size=$PageSize",
  "--lookback-minutes=$Lookback",
  "--overlap-minutes=$Overlap"
)
if ($ScanAgentName) {
  $JobArgList += "--scan-gen-ai-agent-name=$ScanAgentName"
}
if ($IncludeAgentWithoutEvalLabels) {
  $JobArgList += "--include-non-evaluated-agent-traces"
}
$JoinedArgs = $JobArgList -join ","

gcloud run jobs deploy $JobName `
  --project $Project `
  --region $Region `
  --image $Image `
  --service-account $RuntimeSa `
  --task-timeout $TaskTimeout `
  --max-retries 1 `
  --set-env-vars $JoinedEnv `
  --args="$JoinedArgs"

gcloud run jobs add-iam-policy-binding $JobName --project $Project --region $Region --member "serviceAccount:$SchedulerSa" --role roles/run.invoker *> $null

$RunUri = "https://run.googleapis.com/v2/projects/$Project/locations/$Region/jobs/$JobName`:run"
try {
    gcloud scheduler jobs describe $SchedulerJobName --location $Region --project $Project *> $null
    gcloud scheduler jobs update http $SchedulerJobName --location $Region --project $Project --schedule $Schedule --time-zone $TimeZone --uri $RunUri --http-method POST --oauth-service-account-email $SchedulerSa --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform"
} catch {
    gcloud scheduler jobs create http $SchedulerJobName --location $Region --project $Project --schedule $Schedule --time-zone $TimeZone --uri $RunUri --http-method POST --oauth-service-account-email $SchedulerSa --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform"
}

gcloud run jobs execute $JobName --project $Project --region $Region --wait
Write-Host "Done: Cloud Run Job=$JobName, Scheduler=$SchedulerJobName"
