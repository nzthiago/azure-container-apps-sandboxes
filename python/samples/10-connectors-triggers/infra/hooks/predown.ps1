#!/usr/bin/env pwsh
# Predown hook (Windows / cross-platform PowerShell) — Python flavor.
#
# Runs BEFORE azd deletes the Bicep-managed resource group, so we tear
# down preview-API resources (connector gateway + connections + trigger
# configs, and our entry on the sandbox group's gatewayConnections[])
# while they're still resolvable. Without this hook, `azd down` would
# delete the RG and orphan a sandbox-group reference to a gateway that
# no longer exists, AND would leave the OAuth connection consent record
# alive (because the consent is tied to the connection's createdBy
# identity, not the RG).
#
# Delegates to setup/teardown.py — the same script the README documents
# — so the azd path and the manual path stay in lock-step.

$ErrorActionPreference = "Continue"
Set-StrictMode -Version Latest

$scenarioTeardown = Join-Path $PSScriptRoot "../../setup/teardown.py"
$scenarioReqs     = Join-Path $PSScriptRoot "../../setup/requirements.txt"

function Get-AzdEnv {
    param([string]$Key)
    $out = & azd env get-value $Key 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    if (-not $out) { return $null }
    $out.Trim()
}

# Resolve python (match postprovision.ps1's "python" requirement).
$python = $null
foreach ($cand in @("python", "python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $python = $cand; break }
}
if (-not $python) {
    Write-Host "==> Python not found on PATH; skipping connector teardown (azd down will still delete the RG)." -ForegroundColor Yellow
    exit 0
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Host "==> az CLI not found on PATH; skipping connector teardown." -ForegroundColor Yellow
    exit 0
}
try {
    $azAccount = az account show -o json | ConvertFrom-Json
} catch {
    Write-Host "==> az CLI is not logged in; skipping connector teardown." -ForegroundColor Yellow
    exit 0
}

# Point az at the right subscription.
$sub = $env:AZURE_SUBSCRIPTION_ID
if (-not $sub) { $sub = (Get-AzdEnv "AZURE_SUBSCRIPTION_ID") }
if ($sub -and $azAccount.id -ne $sub) {
    Write-Host "==> Pointing az CLI at subscription $sub (was $($azAccount.id))" -ForegroundColor Yellow
    az account set --subscription $sub | Out-Null
}

# Mirror azd-env overrides into child env so teardown.py sees them even
# if .env has been purged.
foreach ($k in @(
    "ACA_SANDBOX_GROUP",
    "ACA_CONNECTOR_GATEWAY",
    "ACA_CONNECTOR_GATEWAY_REGION",
    "ACA_CONNECTOR_CONNECTION",
    "ACA_RESOURCE_GROUP"
)) {
    $v = (Get-AzdEnv $k)
    if ($v) { Set-Item -Path "env:$k" -Value $v }
}

Write-Host "==> azd predown: tearing down connector resources (gateway + connections + SG wiring)..." -ForegroundColor Cyan
if (-not (Test-Path $scenarioTeardown)) {
    Write-Host "    teardown.py not found at $scenarioTeardown; skipping." -ForegroundColor Yellow
    exit 0
}

if (Test-Path $scenarioReqs) {
    & $python -m pip install --quiet --disable-pip-version-check -r $scenarioReqs *>$null
}

& $python $scenarioTeardown --yes
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> teardown.py exited $LASTEXITCODE; continuing with azd down so the RG is still removed." -ForegroundColor Yellow
}

Write-Host "==> azd predown: done." -ForegroundColor Green
exit 0
