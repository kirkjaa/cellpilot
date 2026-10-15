# One-time project setup before the first deploy. Holds no secrets: credentials come from the
# environment (GOOGLE_APPLICATION_CREDENTIALS, or an existing gcloud login).
#
#   .\setup-cloud.ps1 -Project <project-id> -Bucket <store-bucket>
param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Bucket,
    [string]$Region = "asia-southeast1",
    [int]$BudgetUsd = 300
)
# Not "Stop": Windows PowerShell 5.1 reports gcloud's progress messages (written to stderr) as errors.
$ErrorActionPreference = "Continue"
function Check($step) { if ($LASTEXITCODE -ne 0) { throw "$step failed (exit $LASTEXITCODE)" } }

if ($env:GOOGLE_APPLICATION_CREDENTIALS) {
    gcloud auth activate-service-account --key-file="$env:GOOGLE_APPLICATION_CREDENTIALS" --quiet
}
gcloud config set project $Project --quiet
gcloud config set run/region $Region --quiet

# APIs the service needs: Cloud Run and its source builds, the bucket, and Gemini Enterprise Agent Platform (aiplatform) for Argon.
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    storage.googleapis.com aiplatform.googleapis.com --quiet

# The store: results, sign-offs and clips too big to send inline. Uploads expire after 14 days.
if (-not (gcloud storage buckets list --filter="name:$Bucket" --format="value(name)")) {
    gcloud storage buckets create "gs://$Bucket" --location=$Region --uniform-bucket-level-access --quiet
}
$lifecycle = Join-Path $env:TEMP "cellpilot-lifecycle.json"
'{"rule":[{"action":{"type":"Delete"},"condition":{"age":14,"matchesPrefix":["media/"]}}]}' | Set-Content $lifecycle -Encoding ascii
gcloud storage buckets update "gs://$Bucket" --lifecycle-file=$lifecycle --quiet

Write-Host "Project $Project ready: APIs enabled, bucket gs://$Bucket in $Region."
Write-Host "Budget alerts (US`$$BudgetUsd) are set in the console under Billing > Budgets; they warn, they do not stop spend."
