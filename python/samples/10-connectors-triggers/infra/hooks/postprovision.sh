#!/usr/bin/env sh
# Postprovision hook (POSIX shells: bash, zsh, sh).
#
# azd has created the resource group via infra/main.bicep. This hook
# delegates the rest (preview-API resources + OAuth consent) to the
# same setup/setup.py that the README documents, so the azd path and
# the manual path stay in lock-step.
#
# The sandbox group is named via ACA_SANDBOX_GROUP (default
# 'ai-apps-samples-group') and is auto-created in the resource group by
# setup.py if it doesn't already exist.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCENARIO_SETUP="$SCRIPT_DIR/../../setup/setup.py"
SCENARIO_REQS="$SCRIPT_DIR/../../setup/requirements.txt"

azd_get() {
    out="$(azd env get-value "$1" 2>/dev/null)" || return 0
    [ -z "$out" ] && return 0
    printf '%s' "$out"
}

require_tool() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: required CLI '$1' not found on PATH. $2" >&2
        exit 1
    fi
}
require_tool az "Install: https://learn.microsoft.com/cli/azure/install-azure-cli"
require_tool aca "Install: https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md"

# Resolve python interpreter ONCE (avoid the python3 || python double-run bug).
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "error: neither python3 nor python found on PATH. Install Python 3.10+." >&2
    exit 1
fi

if ! az account show -o tsv --query id >/dev/null 2>&1; then
    echo "error: az CLI is not logged in. Run 'az login' and re-try 'azd up'." >&2
    exit 1
fi

# ----- Resolve subscription + RG (azd is the source of truth) -------------
SUB="${AZURE_SUBSCRIPTION_ID:-$(azd_get AZURE_SUBSCRIPTION_ID)}"
[ -z "$SUB" ] && SUB="$(az account show --query id -o tsv 2>/dev/null || true)"

ACTIVE_SUB="$(az account show --query id -o tsv 2>/dev/null || true)"
if [ -n "$SUB" ] && [ "$ACTIVE_SUB" != "$SUB" ]; then
    echo "==> Pointing az CLI at subscription $SUB (was $ACTIVE_SUB)"
    az account set --subscription "$SUB"
fi

RG="${ACA_RESOURCE_GROUP:-}"
[ -z "$RG" ] && RG="$(azd_get ACA_RESOURCE_GROUP)"
[ -z "$RG" ] && RG="$(azd_get AZURE_RESOURCE_GROUP)"
if [ -z "$RG" ]; then
    echo "error: could not resolve resource group from azd env." >&2
    exit 1
fi

RG_LOCATION="$(az group show --name "$RG" --query location -o tsv 2>/dev/null || true)"
if [ -z "$RG_LOCATION" ]; then
    echo "error: could not read location for resource group '$RG'. Did Bicep deployment succeed?" >&2
    exit 1
fi

SANDBOX_REGIONS="australiaeast brazilsouth canadacentral canadaeast centralus \
eastasia eastus2 francecentral germanywestcentral japaneast koreacentral \
mexicocentral northcentralus northeurope norwayeast polandcentral \
southafricanorth southeastasia southindia spaincentral swedencentral \
switzerlandnorth uksouth westcentralus westus westus2 westus3"
RG_LOCATION_LOWER="$(echo "$RG_LOCATION" | tr '[:upper:]' '[:lower:]')"
case " $SANDBOX_REGIONS " in
    *" $RG_LOCATION_LOWER "*) ;;
    *)
        cat >&2 <<EOF
error: Resource group '$RG' is in region '$RG_LOCATION', which does not
support Microsoft.App/sandboxGroups.

Supported regions:
  $SANDBOX_REGIONS

To recover:
  1. azd down --purge             # removes the bad RG
  2. azd env set AZURE_LOCATION westus2
  3. azd up                       # provisions in a supported region
EOF
        exit 1
        ;;
esac

export ACA_SANDBOXGROUP_REGION="$RG_LOCATION"
export ACA_REGION="$RG_LOCATION"
echo "==> Using RG location '$RG_LOCATION' as sandbox-group region (override with ACA_SANDBOXGROUP_REGION + azd up to change)."

if [ ! -t 0 ]; then
    echo "==> stdin appears to be redirected; OAuth consent flow may fail." >&2
    echo "    If setup.py prompts and exits, re-run 'azd up' from an interactive shell." >&2
fi

echo "==> azd postprovision: provisioning preview-API resources"
echo "    subscription:    $SUB"
echo "    resource group:  $RG"
echo "    (Bicep created only the RG; everything else uses preview APIs"
echo "     for which Bicep types are not yet published.)"
echo

# ----- Seed .env so setup.py finds subscription + RG ----------------------
# Find the repo root by walking up looking for .git.
REPO_ROOT="$SCRIPT_DIR"
while [ "$REPO_ROOT" != "/" ] && [ -n "$REPO_ROOT" ] && [ ! -e "$REPO_ROOT/.git" ]; do
    REPO_ROOT="$(dirname "$REPO_ROOT")"
done
if [ ! -e "$REPO_ROOT/.git" ]; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi
ENV_FILE=""
SEARCH_DIR="$SCRIPT_DIR"
while [ "$SEARCH_DIR" != "/" ] && [ -n "$SEARCH_DIR" ]; do
    if [ -f "$SEARCH_DIR/.env" ]; then ENV_FILE="$SEARCH_DIR/.env"; break; fi
    SEARCH_DIR="$(dirname "$SEARCH_DIR")"
done
if [ -z "$ENV_FILE" ]; then
    ENV_FILE="$REPO_ROOT/.env"
    echo "    no existing .env found; creating $ENV_FILE"
    : > "$ENV_FILE"
fi

set_env_line() {
    [ -z "$3" ] && return 0
    if grep -q "^${2}=" "$1" 2>/dev/null; then
        tmp="$1.tmp.$$"
        awk -v k="$2" -v v="$3" '
            BEGIN { sub_re = "^" k "=" }
            $0 ~ sub_re { print k "=" v; next }
            { print }
        ' "$1" > "$tmp"
        mv "$tmp" "$1"
    else
        printf '%s=%s\n' "$2" "$3" >> "$1"
    fi
}

set_env_line "$ENV_FILE" "AZURE_SUBSCRIPTION_ID" "$SUB"
set_env_line "$ENV_FILE" "ACA_SUBSCRIPTION"      "$SUB"
set_env_line "$ENV_FILE" "ACA_RESOURCE_GROUP"    "$RG"
set_env_line "$ENV_FILE" "ACA_SANDBOXGROUP_REGION" "$RG_LOCATION"
set_env_line "$ENV_FILE" "ACA_REGION"            "$RG_LOCATION"

for k in \
    ACA_SANDBOX_GROUP \
    ACA_CONNECTOR_GATEWAY \
    ACA_CONNECTOR_GATEWAY_REGION \
    ACA_CONNECTOR_CONNECTION \
    ACA_USER_EMAIL
do
    v="$(azd_get "$k")"
    [ -n "$v" ] && export "$k=$v"
done

# ACA_SANDBOX_GROUP is optional - setup.py defaults it to
# 'ai-apps-samples-group' and creates the group (plus a role assignment
# for the current principal) if it doesn't exist.

# ----- Run the scenario setup (Python flow) -------------------------------
echo "==> Connector scenario setup (gateway + connection + OAuth consent)..."
"$PYTHON" -m pip install --quiet --disable-pip-version-check -r "$SCENARIO_REQS"
"$PYTHON" "$SCENARIO_SETUP"

# ----- Mirror .env -> azd env so 'azd env get-values' is rich -------------
# Note: derived values (runtime URL, gateway/SG MI principalIds) are
# DELIBERATELY excluded — they're re-resolved from ARM on every run.py
# invocation. Mirroring them into azd env would let them go stale and
# silently break run.py whenever the connection/gateway/SG is recreated.
echo
echo "==> Mirroring connector keys into azd env..."
MIRROR="ACA_SANDBOX_GROUP ACA_SANDBOXGROUP_REGION ACA_REGION ACA_CONNECTOR_GATEWAY ACA_CONNECTOR_GATEWAY_REGION ACA_CONNECTOR_CONNECTION ACA_USER_EMAIL"
# Defensive cleanup: if an earlier version of this sample mirrored
# derived keys into the azd env, unset them now so they don't shadow
# the ARM-resolved values at run-time.
for stale in ACA_CONNECTOR_GATEWAY_PRINCIPAL_ID ACA_CONNECTOR_GATEWAY_TENANT_ID ACA_CONNECTOR_CONNECTION_RUNTIME_URL ACA_SANDBOX_GROUP_PRINCIPAL_ID; do
    azd env set "$stale" "" >/dev/null 2>&1 || true
done
while IFS= read -r line; do
    case "$line" in
        ''|\#*) continue ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    if [ -z "$key" ] || [ "$key" = "$line" ]; then continue; fi
    for m in $MIRROR; do
        if [ "$key" = "$m" ] && [ -n "$val" ]; then
            azd env set "$key" "$val" >/dev/null
            break
        fi
    done
done < "$ENV_FILE"

echo
echo "==> azd postprovision: done."
echo
echo "Next, fire the end-to-end demo with:"
echo "  cd feedback-analyzer && pip install -r requirements.txt && python run.py"
