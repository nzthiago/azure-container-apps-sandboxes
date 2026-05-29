#!/usr/bin/env bash
# Connector-gateway scenario - CLI setup (pure bash + az).
#
# Provisions:
#   1. Connector gateway with SystemAssigned MI
#      (Microsoft.Web/connectorGateways, ARM PUT via `az rest`)
#   2. Office 365 connection on the gateway
#   3. One-time OAuth consent flow (if connection isn't already Connected)
#   4. Access policy: gateway MI -> connection
#   5. Sandbox-group SystemAssigned MI + send-side access policy
#      (sandbox MI -> connection)
#   6. Declarative wiring on the sandbox group:
#      PATCH properties.gatewayConnections[] with
#      { resourceId, connectionRuntimeUrl, authentication.type=SystemAssignedManagedIdentity }.
#      Once this entry exists (and per-sandbox gatewayConnections lists
#      reference the same connection), the platform injects Bearer auth
#      automatically on every outbound call to the runtime URL.
#   7. Appends gateway / connection keys to .env
#
# Flags:
#   --non-interactive    Don't open browser or wait for Enter. Exits with
#                        code 2 if OAuth consent is still required; re-run
#                        after completing consent.
#
# Prerequisites:
#   * Sandbox group: if ACA_SANDBOX_GROUP is set, that group is used
#     (created in the RG if it doesn't exist); otherwise the default
#     name 'ai-apps-samples-group' is used. The script will try to
#     assign the 'Container Apps SandboxGroup Data Owner' role to the
#     current principal but will continue with a warning if that fails.
#   * `az login` complete.
#
# Override defaults with environment variables:
#   ACA_CONNECTOR_GATEWAY            default: ai-apps-samples-gw
#   ACA_CONNECTOR_GATEWAY_REGION     default: ACA_SANDBOXGROUP_REGION
#                                    (from .env; e.g. westus2)
#   ACA_CONNECTOR_CONNECTION         default: o365-conn

set -euo pipefail

NON_INTERACTIVE=0
for arg in "$@"; do
    case "$arg" in
        --non-interactive|--non_interactive) NON_INTERACTIVE=1 ;;
        -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
        *) echo "error: unknown argument: $arg" >&2; exit 1 ;;
    esac
done

API_VERSION="2026-05-01-preview"
# Sandbox-group ARM resource uses a different (older) API version that
# expresses properties.gatewayConnections[].
SANDBOXGROUP_API_VERSION="2026-02-01-preview"
CONNECTOR_NAME="office365"

: "${ACA_CONNECTOR_GATEWAY:=ai-apps-samples-gw}"
# ACA_CONNECTOR_GATEWAY_REGION defaults to the sandbox-group region after the
# .env file is sourced below.
: "${ACA_CONNECTOR_CONNECTION:=o365-conn}"

# Prevent MSYS path mangling on Windows Git Bash (we pass /subscriptions/...
# to az rest --url and would otherwise get mangled into C:\Program Files\...).
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# ----- prereq: az login --------------------------------------------------
if ! command -v az >/dev/null 2>&1; then
    echo "error: azure CLI ('az') not found on PATH." >&2
    exit 1
fi

# ----- transient-retry wrapper for `az rest` -----------------------------
# Retries up to 5 times with exponential backoff (2,4,8,16s) on ARM
# control-plane blips (HTTP 429/502/503/504, "Service Unavailable",
# gateway-timeout, etc.) so a brief outage doesn't fail the whole setup.
# Stdout/exit-code passthrough — drop-in replacement for `az rest <args>`.
az_rest_retry() {
    local attempt=1 max_attempts=5 delay rc err_file err
    err_file="$(mktemp -t aca-trig-azr-XXXXXX 2>/dev/null || mktemp)"
    while :; do
        if az rest "$@" 2>"$err_file"; then
            rm -f "$err_file"
            return 0
        fi
        rc=$?
        err="$(cat "$err_file" 2>/dev/null || true)"
        # shellcheck disable=SC2076
        if [[ "$attempt" -lt "$max_attempts" ]] && \
           [[ "$err" =~ (Service\ Unavailable|Temporarily\ Unavailable|Gateway\ Timeout|Bad\ Gateway|\(503\)|\(502\)|\(504\)|\(429\)|TooManyRequests|internal\ server\ error|Request\ timed\ out) ]]; then
            delay=$((2 ** attempt))
            (( delay > 16 )) && delay=16
            echo "    warning: az rest returned transient ARM error (attempt $attempt/$max_attempts); retrying in ${delay}s..." >&2
            sleep "$delay"
            attempt=$((attempt + 1))
            continue
        fi
        # Real (non-transient) failure - surface captured stderr and bail.
        cat "$err_file" >&2
        rm -f "$err_file"
        return "$rc"
    done
}

SUB="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv 2>/dev/null || true)}"
if [[ -z "$SUB" ]]; then
    echo "error: not logged in to Azure. Run 'az login' first." >&2
    exit 1
fi

# ----- load .env (walk up to find one written by an upstream setup) ---------
# Matches the .env-discovery pattern other samples in this repo use
# (e.g. python/samples/00-get-started/01-sandboxes/sandboxes.py).
_find_env_file() {
    local d
    d="$(cd "$(dirname "$0")" && pwd)"
    while [[ "$d" != "/" && -n "$d" ]]; do
        if [[ -f "$d/.env" ]]; then echo "$d/.env"; return 0; fi
        d="$(dirname "$d")"
    done
    # Fall back to repo root if no .env exists yet
    d="$(cd "$(dirname "$0")" && pwd)"
    while [[ "$d" != "/" && -n "$d" ]]; do
        if [[ -e "$d/.git" ]]; then echo "$d/.env"; return 0; fi
        d="$(dirname "$d")"
    done
    return 1
}
ENV_FILE="$(_find_env_file)" || { echo "error: could not determine .env location." >&2; exit 1; }
SAMPLES_DIR="$(dirname "$ENV_FILE")"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "error: $ENV_FILE not found." >&2
    echo "       Run 'azd up' from this directory first to create one." >&2
    exit 1
fi
# shellcheck disable=SC1090
# Preserve any pre-set ACA_SANDBOX_GROUP across sourcing the env file so
# the caller's choice always wins over the on-disk default.
_PRE_SANDBOX_GROUP="${ACA_SANDBOX_GROUP:-}"
set -a; source "$ENV_FILE"; set +a
if [[ -n "$_PRE_SANDBOX_GROUP" ]]; then
    ACA_SANDBOX_GROUP="$_PRE_SANDBOX_GROUP"
fi

if [[ -z "${ACA_RESOURCE_GROUP:-}" ]]; then
    echo "error: ACA_RESOURCE_GROUP not in $ENV_FILE." >&2
    echo "       Provision a resource group first (e.g. via 'azd up')." >&2
    exit 1
fi

# ----- Resolve / prompt for ACA_USER_EMAIL ----------------------------------
# Needed by feedback-analyzer/run.sh as the default destination for the
# AI-composed reply. It MUST match the mailbox you consent with in the
# next step (since both directions use the same connection). Ask now so
# the demo step doesn't bail later with "TRIAGE_RECIPIENT not set and
# ACA_USER_EMAIL is empty".
if [[ -z "${ACA_USER_EMAIL:-}" ]]; then
    if [[ "$NON_INTERACTIVE" == "1" ]]; then
        echo "warning: ACA_USER_EMAIL not set and running --non-interactive;" >&2
        echo "         feedback-analyzer/run.sh will require TRIAGE_RECIPIENT or" >&2
        echo "         ACA_USER_EMAIL to be set before it can run." >&2
    else
        echo
        echo "==> Office 365 mailbox required"
        echo "    The next step pops an OAuth-consent link for an O365 mailbox."
        echo "    The feedback-analyzer demo then uses that mailbox to send a test"
        echo "    'Feedback' email AND as the default destination for the reply."
        echo "    Enter the email (UPN) of that mailbox now — it must match the"
        echo "    account you sign in with at the consent step."
        while :; do
            read -r -p "    Office 365 email: " _email || true
            _email="$(printf '%s' "${_email:-}" | tr -d '[:space:]')"
            if [[ "$_email" == *"@"*"."* ]]; then
                ACA_USER_EMAIL="$_email"
                export ACA_USER_EMAIL
                break
            fi
            echo "    invalid; expected e.g. you@contoso.com"
        done
        unset _email
    fi
fi

# Default gateway region from the sandbox-group region (canonical key first,
# then the legacy ACA_REGION alias).
: "${ACA_CONNECTOR_GATEWAY_REGION:=${ACA_SANDBOXGROUP_REGION:-${ACA_REGION:-}}}"
if [[ -z "$ACA_CONNECTOR_GATEWAY_REGION" ]]; then
    echo "error: could not determine gateway region. Either set" >&2
    echo "       ACA_CONNECTOR_GATEWAY_REGION explicitly, or set" >&2
    echo "       ACA_SANDBOXGROUP_REGION in $ENV_FILE." >&2
    exit 1
fi

GW="$ACA_CONNECTOR_GATEWAY"
REGION="$ACA_CONNECTOR_GATEWAY_REGION"
CONN="$ACA_CONNECTOR_CONNECTION"
GW_URL_BASE="https://management.azure.com/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.Web/connectorGateways/$GW"

# ----- Resolve / create sandbox group ------------------------------------
# Default the sandbox group name if the caller didn't provide one.
ACA_SANDBOX_GROUP_DEFAULT="ai-apps-samples-group"
if [[ -z "${ACA_SANDBOX_GROUP:-}" ]]; then
    ACA_SANDBOX_GROUP="$ACA_SANDBOX_GROUP_DEFAULT"
    echo "==> ACA_SANDBOX_GROUP not set; using default '$ACA_SANDBOX_GROUP'."
fi
export ACA_SANDBOX_GROUP

ensure_sandbox_group() {
    # Ensure the named sandbox group exists in the configured RG. If it
    # already exists, prints its location to stdout (so the caller can
    # colocate the gateway with it). If it doesn't exist, creates one in
    # $REGION and prints $REGION. Fails fast on aca CLI errors that are
    # not 'group not found'.
    local sg="$1" want_region="$2"
    local existing_loc
    # Probe via ARM directly so we can use --query for clean stdout. The
    # aca CLI's `sandboxgroup get` doesn't support --query and prints a
    # table by default, which is awkward to parse in bash.
    local sg_url="https://management.azure.com/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.App/sandboxGroups/$sg?api-version=$SANDBOXGROUP_API_VERSION"
    existing_loc="$(az_rest_retry --method GET --url "$sg_url" --query "location" -o tsv 2>/dev/null || true)"
    if [[ -n "$existing_loc" ]]; then
        echo "    sandbox group '$sg' already exists (location=$existing_loc)" >&2
        printf '%s' "$existing_loc"
        return 0
    fi
    echo "==> Creating sandbox group '$sg' in $want_region..." >&2
    if ! aca sandboxgroup create --name "$sg" --location "$want_region" >/dev/null; then
        echo "error: aca sandboxgroup create failed. Check that:" >&2
        echo "       * ACA_SUBSCRIPTION=$SUB is correct and you have access," >&2
        echo "       * ACA_RESOURCE_GROUP=$ACA_RESOURCE_GROUP exists in '$want_region'," >&2
        echo "       * '$want_region' supports Microsoft.App/sandboxGroups." >&2
        exit 1
    fi
    printf '%s' "$want_region"
}

ensure_sandbox_group_role() {
    # Try to grant Data Owner role to the current principal. Warn (don't
    # fail) on errors — the user may already have it via inheritance, or
    # may lack permission to grant it but still have it via another
    # mechanism. Subsequent data-plane calls will surface 403s clearly.
    local sg="$1" principal_id
    principal_id="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
    if [[ -z "$principal_id" ]]; then
        principal_id="$(az account show --query user.assignedIdentityInfo -o tsv 2>/dev/null || true)"
    fi
    if [[ -z "$principal_id" ]]; then
        echo "    warning: could not detect current principal; skipping role assignment." >&2
        echo "    If later calls hit 403, run manually with your principal id:" >&2
        echo "      aca sandboxgroup role create --name '$sg' \\" >&2
        echo "        --role 'Container Apps SandboxGroup Data Owner' \\" >&2
        echo "        --principal-id <your-principal-id>" >&2
        return 0
    fi
    echo "    granting 'Container Apps SandboxGroup Data Owner' to $principal_id on '$sg'..." >&2
    if ! aca sandboxgroup role create \
            --name "$sg" \
            --role "Container Apps SandboxGroup Data Owner" \
            --principal-id "$principal_id" >/dev/null 2>&1; then
        echo "    note: role assignment did not complete (may already exist, or insufficient perms)." >&2
        echo "    If later calls hit 403, run manually:" >&2
        echo "      aca sandboxgroup role create --name '$sg' \\" >&2
        echo "        --role 'Container Apps SandboxGroup Data Owner' \\" >&2
        echo "        --principal-id $principal_id" >&2
    fi
}

ACA_SANDBOX_GROUP_LOCATION="$(ensure_sandbox_group "$ACA_SANDBOX_GROUP" "$REGION")"
ensure_sandbox_group_role "$ACA_SANDBOX_GROUP"
if [[ -n "$ACA_SANDBOX_GROUP_LOCATION" && "$ACA_SANDBOX_GROUP_LOCATION" != "$REGION" ]]; then
    echo "    note: sandbox group is in '$ACA_SANDBOX_GROUP_LOCATION'; colocating gateway there (was '$REGION')."
    REGION="$ACA_SANDBOX_GROUP_LOCATION"
    ACA_CONNECTOR_GATEWAY_REGION="$REGION"
fi
ACA_SANDBOXGROUP_REGION="$ACA_SANDBOX_GROUP_LOCATION"
export ACA_SANDBOXGROUP_REGION

echo "==> Connector-gateway scenario - CLI setup"
echo "    subscription:    $SUB"
echo "    resource group:  $ACA_RESOURCE_GROUP"
echo "    gateway:         $GW"
echo "    gateway region:  $REGION"
echo "    connection:      $CONN ($CONNECTOR_NAME)"

# ----- temp-file helper (we register cleanup once with trap) -------------
TMPDIR_S="${TMPDIR:-/tmp}"
TMPFILES=()
mktmp() {
    local f
    f="$(mktemp "$TMPDIR_S/aca-trig-XXXXXX.json")"
    TMPFILES+=("$f")
    printf '%s' "$f"
}
cleanup_tmp() { rm -f "${TMPFILES[@]:-}" 2>/dev/null || true; }
trap cleanup_tmp EXIT

# Translate a POSIX temp path to a Windows path on Git Bash / MSYS when
# MSYS path conversion is disabled (see MSYS_NO_PATHCONV above). Without
# this, `az rest --body @/tmp/foo.json` is passed verbatim to the Windows
# az.cmd, which cannot resolve /tmp/... and treats the whole "@..."
# token as the literal body content -> "Unexpected character @" 400.
# On Linux / macOS cygpath is absent, so we return the path unchanged.
_body_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s' "$1"
    fi
}

# Resolve a working Python interpreter. On Windows hosts the binary is
# normally `python` (not `python3`); macOS / most Linux distros ship
# `python3`. We probe both and bail early with a clear error if neither
# is on PATH. Note that the Windows "python3" shim from the Microsoft
# Store is a stub that prints an install hint to stderr and exits 9009
# without running anything - so we treat it as missing.
_PY=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1; then
        if "$_cand" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 7) else 1)" >/dev/null 2>&1; then
            _PY="$_cand"
            break
        fi
    fi
done
if [[ -z "$_PY" ]]; then
    echo "error: need Python 3.7+ on PATH (tried 'python3' and 'python')." >&2
    exit 1
fi

# ----- 1. Connector gateway ----------------------------------------------
echo "==> Ensuring connector gateway '$GW' in $REGION..."
GW_BODY_FILE="$(mktmp)"
cat > "$GW_BODY_FILE" <<EOF
{"location":"$REGION","identity":{"type":"SystemAssigned"}}
EOF
az_rest_retry \
    --method PUT \
    --url "$GW_URL_BASE?api-version=$API_VERSION" \
    --headers "Content-Type=application/json" \
    --body "@$(_body_path "$GW_BODY_FILE")" >/dev/null

PRINCIPAL_ID="$(az_rest_retry \
    --method GET \
    --url "$GW_URL_BASE?api-version=$API_VERSION" \
    --query "identity.principalId" -o tsv)"
TENANT_ID="$(az_rest_retry \
    --method GET \
    --url "$GW_URL_BASE?api-version=$API_VERSION" \
    --query "identity.tenantId" -o tsv)"
if [[ -z "$PRINCIPAL_ID" || -z "$TENANT_ID" ]]; then
    echo "error: gateway has no system-assigned identity." >&2
    exit 1
fi
echo "    gateway MI principalId=$PRINCIPAL_ID"
echo "    gateway MI tenantId   =$TENANT_ID"

# ----- 2. Connection -----------------------------------------------------
echo "==> Ensuring '$CONNECTOR_NAME' connection '$CONN'..."
CONN_BODY_FILE="$(mktmp)"
cat > "$CONN_BODY_FILE" <<EOF
{"location":"$REGION","properties":{"connectorName":"$CONNECTOR_NAME"}}
EOF
az_rest_retry \
    --method PUT \
    --url "$GW_URL_BASE/connections/$CONN?api-version=$API_VERSION" \
    --headers "Content-Type=application/json" \
    --body "@$(_body_path "$CONN_BODY_FILE")" >/dev/null

# ----- 3. Consent (if needed) --------------------------------------------
read_status() {
    az_rest_retry \
        --method GET \
        --url "$GW_URL_BASE/connections/$CONN?api-version=$API_VERSION" \
        --query "properties.statuses[0].status" -o tsv
}
STATUS="$(read_status)"
[[ -z "$STATUS" ]] && STATUS="Unknown"
echo "    connection status: $STATUS"

if [[ "$STATUS" != "Connected" ]]; then
    # Pull objectId/tenantId off the connection's createdBy block.
    CB_OBJ="$(az_rest_retry \
        --method GET \
        --url "$GW_URL_BASE/connections/$CONN?api-version=$API_VERSION" \
        --query "properties.createdBy.name" -o tsv 2>/dev/null || true)"
    if [[ -z "$CB_OBJ" ]]; then
        CB_OBJ="$(az_rest_retry \
            --method GET \
            --url "$GW_URL_BASE/connections/$CONN?api-version=$API_VERSION" \
            --query "properties.createdBy.objectId" -o tsv 2>/dev/null || true)"
    fi
    CB_TEN="$(az_rest_retry \
        --method GET \
        --url "$GW_URL_BASE/connections/$CONN?api-version=$API_VERSION" \
        --query "properties.createdBy.tenantId" -o tsv)"
    if [[ -z "$CB_OBJ" || -z "$CB_TEN" ]]; then
        echo "error: connection has no createdBy.{name,tenantId}; cannot build consent link." >&2
        exit 1
    fi

    CONSENT_BODY_FILE="$(mktmp)"
    cat > "$CONSENT_BODY_FILE" <<EOF
{"parameters":[{"objectId":"$CB_OBJ","tenantId":"$CB_TEN","redirectUrl":"https://microsoft.com","parameterName":"token"}]}
EOF
    LINK="$(az_rest_retry \
        --method POST \
        --url "$GW_URL_BASE/connections/$CONN/listConsentLinks?api-version=$API_VERSION" \
        --headers "Content-Type=application/json" \
        --body "@$(_body_path "$CONSENT_BODY_FILE")" \
        --query "value[0].link" -o tsv)"
    if [[ -z "$LINK" ]]; then
        echo "error: listConsentLinks returned no link." >&2
        exit 1
    fi

    echo
    echo "========================================================================"
    echo "Office 365 connection needs OAuth consent."
    echo
    echo "  1. The link below is short-lived - click it IMMEDIATELY."
    echo "  2. Sign in with the account whose inbox you want to wire."
    echo "  3. After you see 'You may close this window', return here."
    echo
    echo "  Consent URL:"
    echo "  $LINK"
    echo "========================================================================"

    # Best-effort browser launch (Windows / WSL / macOS / Linux).
    # We do this in BOTH interactive and --non-interactive modes - the URL
    # is short-lived (~5 min) so we want to give the user a fighting chance
    # to see it even when automation is driving the script.
    _opened=0
    if command -v cmd.exe >/dev/null 2>&1; then
        if cmd.exe /c start "" "$LINK" >/dev/null 2>&1; then _opened=1; fi
    elif command -v open >/dev/null 2>&1; then
        if open "$LINK" >/dev/null 2>&1; then _opened=1; fi
    elif command -v xdg-open >/dev/null 2>&1; then
        if xdg-open "$LINK" >/dev/null 2>&1; then _opened=1; fi
    elif command -v powershell.exe >/dev/null 2>&1; then
        if powershell.exe -NoProfile -Command "Start-Process '$LINK'" >/dev/null 2>&1; then _opened=1; fi
    fi
    if [[ "$_opened" == "1" ]]; then
        echo "    opened consent URL in your default browser."
    else
        echo "    could not auto-open browser; copy the URL above manually."
    fi

    if [[ "$NON_INTERACTIVE" == "1" ]]; then
        echo
        echo "--non-interactive set; not waiting for consent to complete."
        echo "Complete consent in the browser, then re-run this script."
        exit 2
    fi

    read -r -p "Press Enter once consent is complete... " _ || true

    for _ in 1 2 3 4 5 6; do
        STATUS="$(read_status)"
        [[ "$STATUS" == "Connected" ]] && break
        sleep 5
    done
    if [[ "$STATUS" != "Connected" ]]; then
        echo "error: connection still shows status '$STATUS'. Re-run setup; the consent link expires quickly." >&2
        exit 1
    fi
    echo "    connection status: $STATUS"
fi

# ----- 4. Access policy: gateway MI -> connection ------------------------
ensure_acl() {
    # ensure_acl <policy_name> <principal_id>
    local _name="$1"
    local _principal="$2"
    local _body_file
    _body_file="$(mktmp)"
    cat > "$_body_file" <<EOF
{"location":"$REGION","properties":{"principal":{"type":"ActiveDirectory","identity":{"objectId":"$_principal","tenantId":"$TENANT_ID"}}}}
EOF
    local _err
    if _err="$(az_rest_retry \
        --method PUT \
        --url "$GW_URL_BASE/connections/$CONN/accessPolicies/$_name?api-version=$API_VERSION" \
        --headers "Content-Type=application/json" \
        --body "@$(_body_path "$_body_file")" 2>&1 >/dev/null)"; then
        echo "    access policy '$_name' applied"
    elif [[ "$_err" == *Exists* || "$_err" == *Conflict* ]]; then
        echo "    access policy '$_name' already exists (skipping)"
    else
        echo "error: access-policy '$_name' PUT failed:" >&2
        echo "$_err" >&2
        exit 1
    fi
}

echo "==> Granting gateway MI access policy on its own connection..."
ensure_acl "gateway-acl" "$PRINCIPAL_ID"

# ----- 4b. Sandbox-group SystemAssigned MI + send-side access policy -----
if ! command -v aca >/dev/null 2>&1; then
    echo "error: the 'aca' CLI is required for this setup but was not found on PATH." >&2
    echo "       Install it and retry:" >&2
    echo "         https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md" >&2
    exit 1
fi

echo "==> Ensuring sandbox-group '$ACA_SANDBOX_GROUP' has SystemAssigned identity..."
read_sg_principal() {
    # `aca sandboxgroup identity show` returns identity-only JSON
    # ({principalId, tenantId, type}). Exits non-zero (with no JSON) if
    # the group has no identity yet — which is what we're probing for.
    # The resource group + subscription are read from `aca` CLI config
    # (or the ACA_RESOURCE_GROUP / ACA_SUBSCRIPTION env vars already in
    # .env).
    aca sandboxgroup identity show --name "$ACA_SANDBOX_GROUP" -o json 2>/dev/null \
        | grep -oE '"principalId"[^"]*"[0-9a-fA-F-]+"' \
        | head -n1 \
        | sed -E 's/.*"([0-9a-fA-F-]+)".*/\1/'
}
SG_PRINCIPAL_ID="$(read_sg_principal || true)"
if [[ -z "$SG_PRINCIPAL_ID" ]]; then
    echo "    enabling SystemAssigned identity on '$ACA_SANDBOX_GROUP'..."
    aca sandboxgroup identity assign \
        --name "$ACA_SANDBOX_GROUP" --system-assigned \
        >/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
        SG_PRINCIPAL_ID="$(read_sg_principal || true)"
        [[ -n "$SG_PRINCIPAL_ID" ]] && break
        sleep 2
    done
fi
if [[ -z "$SG_PRINCIPAL_ID" ]]; then
    echo "error: sandbox-group has no principalId after identity assign." >&2
    exit 1
fi
echo "    sandbox-group MI principalId=$SG_PRINCIPAL_ID"

echo "==> Granting sandbox-group MI access policy on the same connection (send-side)..."
ensure_acl "sandbox-acl" "$SG_PRINCIPAL_ID"

# ----- 4c. Fetch connection runtime URL ----------------------------------
echo "==> Resolving connection runtime URL..."
# Poll for up to 60s — connectionRuntimeUrl may not be set immediately
# after consent completes (the control plane mints it once the OAuth
# secret is in place).
RUNTIME_URL=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    RUNTIME_URL="$(az_rest_retry \
        --method GET \
        --url "$GW_URL_BASE/connections/$CONN?api-version=$API_VERSION" \
        --query "properties.connectionRuntimeUrl" -o tsv 2>/dev/null || true)"
    if [[ -n "$RUNTIME_URL" && "$RUNTIME_URL" != "null" ]]; then break; fi
    sleep 3
done
if [[ -z "$RUNTIME_URL" || "$RUNTIME_URL" == "null" ]]; then
    echo "error: connection '$CONN' has no properties.connectionRuntimeUrl after 60s." >&2
    echo "       Wait ~30s and re-run setup." >&2
    exit 1
fi
RUNTIME_URL="${RUNTIME_URL%/}"
echo "    connectionRuntimeUrl: $RUNTIME_URL"

# ----- 4d. Wire connection on the sandbox group --------------------------
# GET-merge-PATCH properties.gatewayConnections[] on the sandbox group so
# it contains an entry { resourceId, connectionRuntimeUrl, authentication }
# for our connection, without clobbering any pre-existing entries (e.g.
# MCP servers added by other samples). With this entry in place AND each
# sandbox declaring the same resourceId in its own create body, the
# platform injects Bearer auth on every outbound call to the runtime URL
# automatically — no per-sandbox egress Transform rule required.
echo "==> Wiring connection on sandbox group '$ACA_SANDBOX_GROUP' (gatewayConnections[])..."
CONNECTION_RESOURCE_ID="/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.Web/connectorGateways/$GW/connections/$CONN"
SG_URL="https://management.azure.com/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.App/sandboxGroups/$ACA_SANDBOX_GROUP?api-version=$SANDBOXGROUP_API_VERSION"

SG_BODY_FILE="$(mktmp)"
az_rest_retry --method GET --url "$SG_URL" > "$SG_BODY_FILE"

SG_PATCH_BODY="$(mktmp)"
# Pass Windows-style paths to the Python interpreter so that on Git Bash
# the Windows python.exe can actually open the /tmp temp file (it doesn't
# know how to resolve POSIX paths). _body_path is a no-op on Linux/macOS.
"$_PY" - "$(_body_path "$SG_BODY_FILE")" "$CONNECTION_RESOURCE_ID" "$RUNTIME_URL" > "$SG_PATCH_BODY" <<'PYEOF'
import json, sys
sg_body_file, resource_id, runtime_url = sys.argv[1], sys.argv[2], sys.argv[3]
with open(sg_body_file, encoding="utf-8") as f:
    sg = json.load(f)
existing = list((sg.get("properties") or {}).get("gatewayConnections") or [])
rid_lower = resource_id.lower()
new_fields = {
    "resourceId": resource_id,
    "connectionRuntimeUrl": runtime_url,
    "authentication": {"type": "SystemAssignedManagedIdentity"},
}
merged = []
replaced = False
for e in existing:
    if (isinstance(e, dict)
            and isinstance(e.get("resourceId"), str)
            and e["resourceId"].lower() == rid_lower):
        # Merge into existing dict so future/unknown fields are preserved
        # across rewrites; resource IDs compared case-insensitively
        # because ARM treats them as such.
        merged.append({**e, **new_fields})
        replaced = True
    else:
        merged.append(e)
if not replaced:
    merged.append(dict(new_fields))
print(json.dumps({"properties": {"gatewayConnections": merged}}))
PYEOF
az_rest_retry --method PATCH --url "$SG_URL" --headers "Content-Type=application/json" --body "@$(_body_path "$SG_PATCH_BODY")" >/dev/null
echo "    sandbox-group gatewayConnections[] now references '$CONN'"

# ----- 5. Write .env ----------------------------------------------------
echo "==> Writing $ENV_FILE..."
declare -A EXISTING
while IFS='=' read -r k v; do
    k="${k//$'\r'/}"
    k="${k%% *}"
    [[ -z "$k" || "${k:0:1}" == "#" ]] && continue
    EXISTING["$k"]="$v"
done < "$ENV_FILE"
EXISTING[ACA_SANDBOX_GROUP]="$ACA_SANDBOX_GROUP"
EXISTING[ACA_SANDBOXGROUP_REGION]="$ACA_SANDBOXGROUP_REGION"
EXISTING[ACA_CONNECTOR_GATEWAY]="$GW"
EXISTING[ACA_CONNECTOR_GATEWAY_REGION]="$REGION"
EXISTING[ACA_CONNECTOR_CONNECTION]="$CONN"
EXISTING[ACA_CONNECTOR_GATEWAY_PRINCIPAL_ID]="$PRINCIPAL_ID"
EXISTING[ACA_CONNECTOR_GATEWAY_TENANT_ID]="$TENANT_ID"
EXISTING[ACA_CONNECTOR_CONNECTION_RUNTIME_URL]="$RUNTIME_URL"
EXISTING[ACA_SANDBOX_GROUP_PRINCIPAL_ID]="$SG_PRINCIPAL_ID"
if [[ -n "${ACA_USER_EMAIL:-}" ]]; then
    EXISTING[ACA_USER_EMAIL]="$ACA_USER_EMAIL"
fi

{
    echo "# Updated by cli/samples/10-connectors-triggers/setup/setup.sh"
    echo "# Re-run scenario setup to update."
    echo ""
    for k in $(printf '%s\n' "${!EXISTING[@]}" | sort); do
        echo "$k=${EXISTING[$k]}"
    done
} > "$ENV_FILE"
echo "    wrote $ENV_FILE"

echo "==> Done."
echo "    Next:"
if [[ "${OS:-}" == "Windows_NT" || "$(uname -s 2>/dev/null)" == MINGW* || "$(uname -s 2>/dev/null)" == MSYS* || "$(uname -s 2>/dev/null)" == CYGWIN* ]]; then
    echo "      ..\\feedback-analyzer\\run.cmd"
else
    echo "      bash ../feedback-analyzer/run.sh"
fi
