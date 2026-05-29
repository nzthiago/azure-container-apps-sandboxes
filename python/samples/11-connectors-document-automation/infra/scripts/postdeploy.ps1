# Thin wrapper — installs the Python deps the orchestration script
# needs into a local venv and runs it.
#
# Doesn't `Activate` the venv (that scopes PATH down to the venv's
# Scripts/ and we lose access to `az.cmd`). Instead we install via
# the venv's pip and run via the venv's python directly, leaving the
# parent session's PATH intact.

$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Venv = Join-Path $Here ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

# Sandbox SDK is an early-access wheel from a microsoft/azure-container-apps
# GitHub release — same wheel scenario 10's receiver uses.
$SandboxSdkWheel = "https://github.com/microsoft/azure-container-apps/releases/download/python-sdk-v0.1.0b1-early-access/azure_containerapps_sandbox-0.1.0b1-py3-none-any.whl"

if (-not (Test-Path $VenvPython)) {
    Write-Host "==> creating postdeploy venv at $Venv"
    python -m venv $Venv
}

& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet `
    azure-identity `
    httpx `
    $SandboxSdkWheel

& $VenvPython (Join-Path $Here "postdeploy.py") @Args
exit $LASTEXITCODE
