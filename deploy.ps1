# Deploy CellPilot to Cloud Run in Singapore. Holds no secrets: fill the placeholders or pass parameters.
#
#   .\deploy.ps1 -Project <project-id> -Bucket <store-bucket> [-ServiceAccount <runtime-sa-email>]
#
# Before the first deploy: gcloud auth login; create the bucket in asia-southeast1; give the runtime
# service account roles/aiplatform.user on the project and roles/storage.objectAdmin on the bucket.
# Cloud Run supplies credentials through that service account, so no key file is deployed.
param(
    [string]$Project = $env:GOOGLE_CLOUD_PROJECT,
    [string]$Bucket = "<STORE_BUCKET>",
    [string]$ServiceAccount = "",
    [string]$Model = "gemini-4-argon",
    [int]$DailyLiveRuns = 10,
    [int]$MinInstances = 0      # 1 on judging days: no cold start
)
$ErrorActionPreference = "Stop"
if (-not $Project) { throw "Pass -Project or set GOOGLE_CLOUD_PROJECT." }
if ($Bucket -like "<*") { throw "Pass -Bucket with the name of the store bucket (no gs:// prefix)." }

$envVars = @(
    "GOOGLE_CLOUD_PROJECT=$Project",
    "GOOGLE_CLOUD_LOCATION=global",
    "CELLPILOT_MODEL=$Model",
    "CELLPILOT_STORE=gs://$Bucket",
    "CELLPILOT_DAILY_LIVE_RUNS=$DailyLiveRuns"
) -join ","

$deployArgs = @(
    "run", "deploy", "cellpilot",
    "--source", ".",
    "--project", $Project,
    "--region", "asia-southeast1",
    "--allow-unauthenticated",
    "--no-cpu-throttling",
    "--max-instances", "1",
    "--min-instances", "$MinInstances",
    "--cpu", "1",
    "--memory", "1Gi",
    "--timeout", "3600",
    "--set-env-vars", $envVars
)
if ($ServiceAccount) { $deployArgs += @("--service-account", $ServiceAccount) }

gcloud @deployArgs
