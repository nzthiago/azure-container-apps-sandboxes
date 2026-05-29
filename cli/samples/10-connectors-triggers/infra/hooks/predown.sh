#!/usr/bin/env sh
# Predown hook (POSIX shells: bash, zsh, sh) — CLI flavor.
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
#
# Idempotent: if .env or trigger keys are missing (e.g. `azd down`
# called before any successful `azd up`), this hook prints a message
# and exits 0 so azd can continue with RG delete.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCENARIO_TEARDOWN="$SCRIPT_DIR/../../setup/teardown.sh"

azd_get() {
    out="$(azd env get-value "$1" 2>/dev/null)" || return 0
    [ -z "$out" ] && return 0
    printf '%s' "$out"
}

if ! command -v az >/dev/null 2>&1; then
    echo "==> az CLI not found on PATH; skipping connector teardown." >&2
    exit 0
fi
if ! command -v bash >/dev/null 2>&1; then
    echo "==> bash not found on PATH; skipping connector teardown (azd down will still delete the RG)." >&2
    exit 0
fi
if ! az account show -o tsv --query id >/dev/null 2>&1; then
    echo "==> az CLI is not logged in; skipping connector teardown." >&2
    exit 0
fi

# Make sure az is pointed at the right subscription so teardown.sh's
# az rest calls hit the right tenant.
SUB="${AZURE_SUBSCRIPTION_ID:-$(azd_get AZURE_SUBSCRIPTION_ID)}"
ACTIVE_SUB="$(az account show --query id -o tsv 2>/dev/null || true)"
if [ -n "$SUB" ] && [ "$ACTIVE_SUB" != "$SUB" ]; then
    echo "==> Pointing az CLI at subscription $SUB (was $ACTIVE_SUB)"
    az account set --subscription "$SUB"
fi

# Mirror azd-env overrides into the child process env so teardown.sh
# can see ACA_SANDBOX_GROUP / ACA_CONNECTOR_GATEWAY / ACA_CONNECTOR_CONNECTION
# even if .env has been purged.
for k in \
    ACA_SANDBOX_GROUP \
    ACA_CONNECTOR_GATEWAY \
    ACA_CONNECTOR_GATEWAY_REGION \
    ACA_CONNECTOR_CONNECTION \
    ACA_RESOURCE_GROUP
do
    v="$(azd_get "$k")"
    [ -n "$v" ] && export "$k=$v"
done

echo "==> azd predown: tearing down connector resources (gateway + connections + SG wiring)..."
if [ ! -f "$SCENARIO_TEARDOWN" ]; then
    echo "    teardown.sh not found at $SCENARIO_TEARDOWN; skipping." >&2
    exit 0
fi

# teardown.sh exits non-zero if .env is missing (= nothing was ever
# provisioned). That's not an error for predown — azd should still
# delete the RG. Surface a friendly note instead of failing the whole
# 'azd down'.
if ! bash "$SCENARIO_TEARDOWN" --yes; then
    rc=$?
    echo "==> teardown.sh exited $rc; continuing with azd down so the RG is still removed." >&2
fi

echo "==> azd predown: done."
