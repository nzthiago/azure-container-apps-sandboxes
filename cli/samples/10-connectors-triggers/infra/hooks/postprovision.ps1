#!/usr/bin/env pwsh
# Postprovision hook (Windows / cross-platform PowerShell).
#
# azd has created the resource group via infra/main.bicep. This hook
# delegates the rest (preview-API resources + OAuth consent) to the
# same setup/setup.sh that the README documents, so the azd path and
# the manual path stay in lock-step.
#
# The sandbox group is named via ACA_SANDBOX_GROUP (default
# 'ai-apps-samples-group') and is auto-created in the resource group by
# setup.sh if it doesn't already exist.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scenarioSetup = Join-Path $PSScriptRoot "../../setup/setup.sh"
# infra/hooks/postprovision.ps1 -> repo root is 6 ".." segments.
$repoRoot = $PSScriptRoot
while ($repoRoot -and -not (Test-Path (Join-Path $repoRoot ".git"))) {
    $parent = Split-Path -Parent $repoRoot
    if (-not $parent -or $parent -eq $repoRoot) { $repoRoot = $null; break }
    $repoRoot = $parent
}
if (-not $repoRoot) {
    $repoRoot = (Resolve-Path "$PSScriptRoot/../..").Path
}

function Get-AzdEnv {
    param([string]$Key)
    $out = & azd env get-value $Key 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    if (-not $out) { return $null }
    $out.Trim()
}

function Require-Tool {
    param([string]$Name, [string]$InstallHint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required CLI '$Name' not found on PATH. $InstallHint"
    }
}

Require-Tool "az" "Install: https://learn.microsoft.com/cli/azure/install-azure-cli"
Require-Tool "aca" "Install: https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md"

# Resolve a bash interpreter. Prefer Git Bash over WSL because WSL bash
# runs inside the Linux filesystem and cannot launch the Windows aca/az
# CLIs directly. Git Bash is a native Windows process and shares PATH
# with PowerShell.
$bashCmd = $null
foreach ($candidate in @(
    "${env:ProgramFiles}\Git\bin\bash.exe",
    "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
    "${env:ProgramFiles}\Git\usr\bin\bash.exe"
)) {
    if (Test-Path $candidate) { $bashCmd = $candidate; break }
}
if (-not $bashCmd) {
    $onPath = Get-Command bash -ErrorAction SilentlyContinue
    if ($onPath) { $bashCmd = $onPath.Source }
}
if (-not $bashCmd) {
    throw "Bash is required to run setup.sh on Windows. Install Git Bash from https://git-scm.com/download/win or use WSL."
}

try {
    $azAccount = az account show -o json | ConvertFrom-Json
} catch {
    throw "az CLI is not logged in. Run 'az login' and re-try 'azd up'."
}

# ----- Resolve subscription + RG (azd is the source of truth) -------------
$sub = $env:AZURE_SUBSCRIPTION_ID
if (-not $sub) { $sub = (Get-AzdEnv "AZURE_SUBSCRIPTION_ID") }
if (-not $sub) { $sub = $azAccount.id }

$activeSub = $azAccount.id
if ($activeSub -ne $sub) {
    Write-Host "==> Pointing az CLI at subscription $sub (was $activeSub)" -ForegroundColor Yellow
    az account set --subscription $sub | Out-Null
}

$rg = $env:ACA_RESOURCE_GROUP
if (-not $rg) { $rg = (Get-AzdEnv "ACA_RESOURCE_GROUP") }
if (-not $rg) { $rg = (Get-AzdEnv "AZURE_RESOURCE_GROUP") }
if (-not $rg) { throw "Could not resolve resource group from azd env." }

$rgLocation = (az group show --name $rg --query location -o tsv 2>$null)
if (-not $rgLocation) {
    throw "Could not read location for resource group '$rg'. Did Bicep deployment succeed?"
}

$sandboxRegions = @(
    'australiaeast','brazilsouth','canadacentral','canadaeast','centralus',
    'eastasia','eastus2','francecentral','germanywestcentral','japaneast',
    'koreacentral','mexicocentral','northcentralus','northeurope','norwayeast',
    'polandcentral','southafricanorth','southeastasia','southindia',
    'spaincentral','swedencentral','switzerlandnorth','uksouth',
    'westcentralus','westus','westus2','westus3'
)
if ($sandboxRegions -notcontains $rgLocation.ToLowerInvariant()) {
    throw @"
Resource group '$rg' is in region '$rgLocation', which does not support
Microsoft.App/sandboxGroups.

Supported regions:
  $($sandboxRegions -join ', ')

To recover:
  1. azd down --purge             # removes the bad RG
  2. azd env set AZURE_LOCATION westus2
  3. azd up                       # provisions in a supported region
"@
}

$env:ACA_SANDBOXGROUP_REGION = $rgLocation
$env:ACA_REGION = $rgLocation
Write-Host "==> Using RG location '$rgLocation' as sandbox-group region (override with ACA_SANDBOXGROUP_REGION + azd up to change)." -ForegroundColor Cyan

if ([Console]::IsInputRedirected) {
    Write-Host "==> stdin appears to be redirected; OAuth consent flow may fail." -ForegroundColor Yellow
    Write-Host "    If setup.sh prompts and exits, re-run 'azd up' from an interactive shell." -ForegroundColor Yellow
}

Write-Host "==> azd postprovision: provisioning preview-API resources" -ForegroundColor Cyan
Write-Host "    subscription:    $sub"
Write-Host "    resource group:  $rg"
Write-Host "    (Bicep created only the RG; everything else uses preview APIs"
Write-Host "     for which Bicep types are not yet published.)"
Write-Host ""

# ----- Locate / create .env -----------------------------------------------
$envFile = $null
$searchDir = $PSScriptRoot
while ($searchDir -and ($searchDir.Length -gt 3)) {
    $candidate = Join-Path $searchDir ".env"
    if (Test-Path $candidate) { $envFile = $candidate; break }
    $parent = Split-Path -Parent $searchDir
    if ($parent -eq $searchDir) { break }
    $searchDir = $parent
}
if (-not $envFile) {
    $envFile = Join-Path $repoRoot ".env"
    Write-Host "    no existing .env found; creating $envFile"
    New-Item -ItemType File -Path $envFile -Force | Out-Null
}

function Set-EnvLine {
    param([string]$Path, [string]$Key, [string]$Value)
    if (-not $Value) { return }
    $content = Get-Content $Path -Raw -ErrorAction SilentlyContinue
    if (-not $content) {
        Set-Content -Path $Path -Value "$Key=$Value`n" -Encoding utf8 -NoNewline
        return
    }
    if ($content -match "(?m)^$([regex]::Escape($Key))=") {
        $rx = New-Object System.Text.RegularExpressions.Regex("(?m)^$([regex]::Escape($Key))=.*$")
        $replacement = [System.Text.RegularExpressions.MatchEvaluator] { param($m) "${Key}=${Value}" }
        $content = $rx.Replace($content, $replacement)
        Set-Content -Path $Path -Value $content -Encoding utf8 -NoNewline
    } else {
        Add-Content -Path $Path -Value "$Key=$Value"
    }
}

Set-EnvLine $envFile "AZURE_SUBSCRIPTION_ID"   $sub
Set-EnvLine $envFile "ACA_SUBSCRIPTION"        $sub
Set-EnvLine $envFile "ACA_RESOURCE_GROUP"      $rg
Set-EnvLine $envFile "ACA_SANDBOXGROUP_REGION" $rgLocation
Set-EnvLine $envFile "ACA_REGION"              $rgLocation

foreach ($k in @(
    "ACA_SANDBOX_GROUP",
    "ACA_CONNECTOR_GATEWAY",
    "ACA_CONNECTOR_GATEWAY_REGION",
    "ACA_CONNECTOR_CONNECTION",
    "ACA_USER_EMAIL"
)) {
    $v = (Get-AzdEnv $k)
    if ($v) { Set-Item -Path "env:$k" -Value $v }
}

# ACA_SANDBOX_GROUP is optional - setup.sh defaults it to
# 'ai-apps-samples-group' and creates the group (plus a role assignment
# for the current principal) if it doesn't exist.

# ----- Run the scenario setup (bash flow) ---------------------------------
Write-Host "==> Connector scenario setup (gateway + connection + OAuth consent)..." -ForegroundColor Cyan
& $bashCmd $scenarioSetup
if ($LASTEXITCODE -ne 0) { throw "setup.sh failed (exit=$LASTEXITCODE)" }

# ----- Mirror .env -> azd env so 'azd env get-values' is rich -------------
Write-Host ""
Write-Host "==> Mirroring connector keys into azd env..."
$mirror = @(
    "ACA_SANDBOX_GROUP",
    "ACA_SANDBOX_GROUP_PRINCIPAL_ID",
    "ACA_SANDBOXGROUP_REGION",
    "ACA_REGION",
    "ACA_CONNECTOR_GATEWAY",
    "ACA_CONNECTOR_GATEWAY_REGION",
    "ACA_CONNECTOR_GATEWAY_PRINCIPAL_ID",
    "ACA_CONNECTOR_GATEWAY_TENANT_ID",
    "ACA_CONNECTOR_CONNECTION",
    "ACA_CONNECTOR_CONNECTION_RUNTIME_URL",
    "ACA_USER_EMAIL"
)
foreach ($line in (Get-Content $envFile)) {
    if ($line -match "^\s*#" -or $line -notmatch "=") { continue }
    $kv = $line -split "=", 2
    $k = $kv[0].Trim()
    $v = $kv[1].Trim()
    if ($mirror -contains $k -and $v) {
        & azd env set $k $v | Out-Null
    }
}

Write-Host ""
Write-Host "==> azd postprovision: done." -ForegroundColor Green
Write-Host ""
Write-Host "Next, fire the end-to-end demo with:"
Write-Host "  feedback-analyzer\run.cmd"
