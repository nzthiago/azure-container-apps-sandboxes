#!/usr/bin/env bash
# Thin wrapper — installs the Python deps the orchestration script
# needs into a local venv and runs it.
#
# Doesn't `activate` the venv (that scopes PATH down to the venv's
# bin/ and we lose access to `az`). Instead we install via the venv's
# pip and run via the venv's python directly, leaving the parent
# shell's PATH intact.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv"

if [[ ! -d "$VENV" ]]; then
    echo "==> creating postdeploy venv at $VENV"
    python3 -m venv "$VENV"
fi

# Sandbox SDK is an early-access wheel from a microsoft/azure-container-apps
# GitHub release — same wheel scenario 10's receiver uses.
SANDBOX_SDK_WHEEL="https://github.com/microsoft/azure-container-apps/releases/download/python-sdk-v0.1.0b1-early-access/azure_containerapps_sandbox-0.1.0b1-py3-none-any.whl"

"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet \
    azure-identity \
    httpx \
    "$SANDBOX_SDK_WHEEL"

exec "$VENV/bin/python" "$HERE/postdeploy.py" "$@"
