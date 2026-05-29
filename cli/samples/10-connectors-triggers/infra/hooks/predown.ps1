#!/usr/bin/env pwsh
# Predown hook (Windows / cross-platform PowerShell) — CLI flavor.
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
# Delegates to setup/teardown.sh — the same script the README documents
# — so the azd path and the manual path stay in lock-step.

$ErrorActionPreference = "Continue"
Set-StrictMode -Version Latest

$scenarioTeardown = Join-Path $PSScriptRoot "../../setup/teardown.sh"

function Get-AzdEnv {
    param([string]$Key)
    $out = & azd env get-value $Key 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    if (-not $out) { return $null }
    $out.Trim()
}

# Resolve a bash interpreter (same logic as postprovision.ps1).
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
    Write-Host "==> bash not found; skipping connector teardown (azd down will still delete the RG)." -ForegroundColor Yellow
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

# Mirror azd-env overrides into child process env so teardown.sh sees
# them even if .env has been purged.
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
    Write-Host "    teardown.sh not found at $scenarioTeardown; skipping." -ForegroundColor Yellow
    exit 0
}

& $bashCmd $scenarioTeardown --yes
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> teardown.sh exited $LASTEXITCODE; continuing with azd down so the RG is still removed." -ForegroundColor Yellow
}

Write-Host "==> azd predown: done." -ForegroundColor Green
exit 0
