# Deploy CellPilot to Cloud Run in Singapore. Holds no secrets: fill the placeholders or pass parameters.
#
#   .\deploy.ps1 -Project <project-id> -Bucket <store-bucket> [-ServiceAccount <runtime-sa-email>]
#               [-CaptureConfig capture/config.cloud.json -EnvFile <KEY=VALUE file with M1SIM_USER and M1SIM_PASSWORD>]
#
# Before the first deploy: gcloud auth login; create the bucket in asia-southeast1; give the runtime
# service account roles/aiplatform.user on the project and roles/storage.objectAdmin on the bucket.
# Cloud Run supplies credentials through that service account, so no key file is deployed.
# -CaptureConfig turns on the capture studio inside the service (a simulated M1 rig on NODAL's emulator
# console); its login comes from -EnvFile, which is read here and handed to Cloud Run through a
# temporary file, never on the command line.
param(
    [string]$Project = $env:GOOGLE_CLOUD_PROJECT,
    [string]$Bucket = "<STORE_BUCKET>",
    [string]$ServiceAccount = "",
    [string]$Model = "gemini-4-argon",
    [int]$DailyLiveRuns = 10,
    [int]$MinInstances = 0,     # 1 on judging days: no cold start
    [int]$Cpu = 1,
    [string]$Memory = "1Gi",
    [string]$CaptureConfig = "",
    [string]$EnvFile = ""
)
# Not "Stop": Windows PowerShell 5.1 reports gcloud's progress messages (written to stderr) as errors.
$ErrorActionPreference = "Continue"
if (-not $Project) { throw "Pass -Project or set GOOGLE_CLOUD_PROJECT." }
if ($Bucket -like "<*") { throw "Pass -Bucket with the name of the store bucket (no gs:// prefix)." }

$envVars = [ordered]@{
    GOOGLE_CLOUD_PROJECT = $Project
    GOOGLE_CLOUD_LOCATION = "global"
    CELLPILOT_MODEL = $Model
    CELLPILOT_STORE = "gs://$Bucket"
    CELLPILOT_DAILY_LIVE_RUNS = "$DailyLiveRuns"
}
if ($CaptureConfig) { $envVars["CELLPILOT_CAPTURE_CONFIG"] = $CaptureConfig }
if ($EnvFile) {
    if (-not (Test-Path $EnvFile)) { throw "EnvFile not found: $EnvFile" }
    foreach ($line in Get-Content $EnvFile) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $envVars[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim().Trim('"')
    }
}

$deployArgs = @(
    "run", "deploy", "cellpilot",
    "--source", ".",
    "--project", $Project,
    "--region", "asia-southeast1",
    "--allow-unauthenticated",
    "--no-cpu-throttling",
    "--max-instances", "1",
    "--min-instances", "$MinInstances",
    "--cpu", "$Cpu",
    "--memory", $Memory,
    "--timeout", "3600"
)
if ($ServiceAccount) { $deployArgs += @("--service-account", $ServiceAccount) }

$tmp = $null
if ($EnvFile) {
    # Secrets go through a file gcloud reads, not through the command line.
    $tmp = [System.IO.Path]::GetTempFileName() + ".yaml"
    $yaml = foreach ($k in $envVars.Keys) { '{0}: "{1}"' -f $k, ($envVars[$k] -replace '"', '\"') }
    Set-Content -Path $tmp -Value $yaml -Encoding ascii
    $deployArgs += @("--env-vars-file", $tmp)
} else {
    $pairs = foreach ($k in $envVars.Keys) { "$k=$($envVars[$k])" }
    $deployArgs += @("--set-env-vars", ($pairs -join ","))
}

try {
    gcloud @deployArgs
} finally {
    if ($tmp -and (Test-Path $tmp)) { Remove-Item $tmp -Force }
}
