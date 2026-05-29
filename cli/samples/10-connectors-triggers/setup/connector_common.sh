#!/usr/bin/env bash
# Shared bash helpers for the 10-connectors-triggers scenario.
#
# Sourced by both ../setup/setup.sh and ../feedback-analyzer/run.sh so
# that ARM re-resolution, ACL repair and preflight drift checks live in
# ONE place — there's no second copy in run.sh to drift from setup.sh.
#
# Purpose: prevent stale-.env drift. setup.sh used to cache derived
# values (runtime URL, gateway MI principalId, SG MI principalId) into
# .env, but those drift the moment the connection / gateway / sandbox
# group is re-created. run.sh would then call a host the platform proxy
# doesn't recognise and the apihub returns 401 missing-authorization-
# header. Both scripts now re-resolve those values on every invocation
# (3 ARM GETs, ~2s) and refuse to proceed if anything is out of sync,
# printing the exact command to fix it.
#
# Source this with:
#   . "$(dirname "$0")/../setup/connector_common.sh"   # from run.sh
#   . "$(dirname "$0")/connector_common.sh"            # from setup.sh
#
# Expects (set by the caller from .env or argv):
#   SUB                        Azure subscription id
#   ACA_RESOURCE_GROUP         resource group
#   ACA_SANDBOX_GROUP          sandbox group name
#   ACA_CONNECTOR_GATEWAY      gateway name
#   ACA_CONNECTOR_CONNECTION   connection name
#
# Provides shell vars (after `cc_resolve_all`):
#   CC_GW_PRINCIPAL_ID      current gateway MI principalId
#   CC_GW_TENANT_ID         current gateway MI tenantId
#   CC_GW_REGION            gateway location
#   CC_SG_PRINCIPAL_ID      current SG MI principalId
#   CC_SG_TENANT_ID         current SG MI tenantId
#   CC_SG_REGION            sandbox group location (use for dataplane endpoint)
#   CC_CONN_STATUS          connection's properties.statuses[0].status
#   CC_CONN_STATUS_ERROR    error message if not Connected (else empty)
#   CC_RUNTIME_URL          current properties.connectionRuntimeUrl (no trailing /)
#   CC_RUNTIME_HOST         host portion of CC_RUNTIME_URL
#   CC_CONN_RESOURCE_ID     full ARM resourceId of the connection
#   CC_SG_GATEWAY_CONNECTIONS_JSON  current SG-level gatewayConnections[] (raw JSON)
#
# Provides functions:
#   cc_az_rest_retry        transient-retry wrapper for `az rest`
#   cc_resolve_all          GET gateway + connection + SG; populate CC_* vars
#   cc_preflight            assert all wiring matches; print remediation on drift
#   cc_ensure_acl_current   PUT an ACL; if name exists with wrong objectId, DELETE+PUT
#   cc_strip_env_keys       remove deprecated derived keys from a .env file
#
# All output to stderr (use process substitution to capture in callers).

if [[ -n "${_CC_LOADED:-}" ]]; then return 0; fi
_CC_LOADED=1

CC_API_VERSION="${CC_API_VERSION:-2026-05-01-preview}"
CC_SANDBOXGROUP_API_VERSION="${CC_SANDBOXGROUP_API_VERSION:-2026-02-01-preview}"

# Deprecated .env keys — the source-of-truth values are re-resolved from
# ARM on every run. Keeping these around as cached strings was the cause
# of the "stale runtime URL → 401" footgun this module exists to prevent.
CC_DEPRECATED_ENV_KEYS=(
    ACA_CONNECTOR_CONNECTION_RUNTIME_URL
    ACA_CONNECTOR_GATEWAY_PRINCIPAL_ID
    ACA_CONNECTOR_GATEWAY_TENANT_ID
    ACA_SANDBOX_GROUP_PRINCIPAL_ID
)

# Transient-retry wrapper for `az rest`. Retries up to 5 times with
# exponential backoff on transient ARM errors (429/502/503/504, timeout,
# "Service Unavailable", etc.). Drop-in replacement for `az rest <args>`.
cc_az_rest_retry() {
    local attempt=1 max_attempts=5 delay rc err_file err
    err_file="$(mktemp -t cc-azr-XXXXXX 2>/dev/null || mktemp)"
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
            echo "    warning: az rest transient ARM error (attempt $attempt/$max_attempts); retry in ${delay}s..." >&2
            sleep "$delay"
            attempt=$((attempt + 1))
            continue
        fi
        cat "$err_file" >&2
        rm -f "$err_file"
        return "$rc"
    done
}

# Resolve a Python interpreter once (we need it for JSON parsing here).
_cc_py() {
    if [[ -n "${_CC_PY:-}" ]]; then printf '%s' "$_CC_PY"; return 0; fi
    local cand
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1 \
                && "$cand" -c 'import sys' >/dev/null 2>&1; then
            _CC_PY="$cand"
            printf '%s' "$_CC_PY"
            return 0
        fi
    done
    echo "error: need Python 3 on PATH (tried 'python3' and 'python')." >&2
    return 1
}

# Re-resolve every derived value from ARM and populate CC_* shell vars.
# Caller must have set SUB, ACA_RESOURCE_GROUP, ACA_SANDBOX_GROUP,
# ACA_CONNECTOR_GATEWAY, ACA_CONNECTOR_CONNECTION beforehand.
#
# Fails fast (returns non-zero) if anything required is missing — e.g.
# gateway has no MI, SG has no MI, or connection doesn't exist.
cc_resolve_all() {
    local py; py="$(_cc_py)" || return 1

    # Strip stray carriage returns that creep in when .env is sourced
    # from a CRLF-encoded file (common on Windows where users edit the
    # file in Notepad/VSCode without "LF" line-ending setting). Without
    # this, $SUB ends with \r and the ARM URLs become invalid.
    SUB="${SUB%$'\r'}"
    ACA_RESOURCE_GROUP="${ACA_RESOURCE_GROUP%$'\r'}"
    ACA_SANDBOX_GROUP="${ACA_SANDBOX_GROUP%$'\r'}"
    ACA_CONNECTOR_GATEWAY="${ACA_CONNECTOR_GATEWAY%$'\r'}"
    ACA_CONNECTOR_CONNECTION="${ACA_CONNECTOR_CONNECTION%$'\r'}"

    local gw_url sg_url conn_url
    gw_url="https://management.azure.com/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.Web/connectorGateways/$ACA_CONNECTOR_GATEWAY?api-version=$CC_API_VERSION"
    conn_url="https://management.azure.com/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.Web/connectorGateways/$ACA_CONNECTOR_GATEWAY/connections/$ACA_CONNECTOR_CONNECTION?api-version=$CC_API_VERSION"
    sg_url="https://management.azure.com/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.App/sandboxGroups/$ACA_SANDBOX_GROUP?api-version=$CC_SANDBOXGROUP_API_VERSION"

    CC_CONN_RESOURCE_ID="/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.Web/connectorGateways/$ACA_CONNECTOR_GATEWAY/connections/$ACA_CONNECTOR_CONNECTION"

    local gw_json conn_json sg_json
    if ! gw_json="$(cc_az_rest_retry --method GET --url "$gw_url" 2>/dev/null)"; then
        echo "error: gateway '$ACA_CONNECTOR_GATEWAY' not found in RG '$ACA_RESOURCE_GROUP'." >&2
        echo "       Re-run 'azd provision' (or 'bash setup/setup.sh') to recreate it." >&2
        return 1
    fi
    if ! conn_json="$(cc_az_rest_retry --method GET --url "$conn_url" 2>/dev/null)"; then
        echo "error: connection '$ACA_CONNECTOR_CONNECTION' not found on gateway '$ACA_CONNECTOR_GATEWAY'." >&2
        echo "       Re-run 'azd provision' (or 'bash setup/setup.sh') to recreate it." >&2
        return 1
    fi
    if ! sg_json="$(cc_az_rest_retry --method GET --url "$sg_url" 2>/dev/null)"; then
        echo "error: sandbox group '$ACA_SANDBOX_GROUP' not found in RG '$ACA_RESOURCE_GROUP'." >&2
        echo "       Re-run 'azd provision' (or 'bash setup/setup.sh') to recreate it." >&2
        return 1
    fi

    # Parse all three blobs in one Python call to keep latency down.
    # `tr -d '\r'` strips the CR that Windows Python text-mode stdout
    # appends to every '\n' (translating '\n' to '\r\n'). Without it, the
    # value captured here ends with '\r' and downstream comparisons fail.
    local parsed
    parsed="$("$py" - <<'PYEOF' "$gw_json" "$conn_json" "$sg_json" "$CC_CONN_RESOURCE_ID" | tr -d '\r'
import json, sys, urllib.parse
gw, conn, sg, rid = json.loads(sys.argv[1] or "{}"), json.loads(sys.argv[2] or "{}"), json.loads(sys.argv[3] or "{}"), sys.argv[4]
def get(d, *path, default=""):
    cur = d
    for p in path:
        if not isinstance(cur, dict): return default
        cur = cur.get(p)
        if cur is None: return default
    return cur if cur is not None else default
gw_principal = get(gw, "identity", "principalId")
gw_tenant    = get(gw, "identity", "tenantId")
gw_region    = get(gw, "location")
sg_principal = get(sg, "identity", "principalId")
sg_tenant    = get(sg, "identity", "tenantId")
sg_region    = get(sg, "location")
statuses     = get(conn, "properties", "statuses", default=[]) or []
status, status_error = "", ""
if statuses and isinstance(statuses[0], dict):
    status = statuses[0].get("status") or ""
    err = statuses[0].get("error") or {}
    if isinstance(err, dict):
        status_error = err.get("message") or err.get("code") or ""
runtime_url = (get(conn, "properties", "connectionRuntimeUrl") or "").rstrip("/")
runtime_host = ""
if runtime_url:
    try:
        runtime_host = urllib.parse.urlparse(runtime_url).hostname or ""
    except Exception:
        runtime_host = ""
sg_gc = get(sg, "properties", "gatewayConnections", default=[]) or []
out = {
    "gw_principal": gw_principal,
    "gw_tenant": gw_tenant,
    "gw_region": gw_region,
    "sg_principal": sg_principal,
    "sg_tenant": sg_tenant,
    "sg_region": sg_region,
    "status": status,
    "status_error": status_error,
    "runtime_url": runtime_url,
    "runtime_host": runtime_host,
    "sg_gc": sg_gc,
}
print(json.dumps(out))
PYEOF
)" || { echo "error: failed to parse ARM responses." >&2; return 1; }

    # Extract scalars with python (avoids bash JSON parsing pitfalls).
    local scalars
    scalars="$("$py" -c "
import json, sys
d = json.loads(sys.stdin.read())
for k in ('gw_principal','gw_tenant','gw_region','sg_principal','sg_tenant','sg_region','status','status_error','runtime_url','runtime_host'):
    print(d.get(k, ''))
" <<<"$parsed" | tr -d '\r')"
    {
        read -r CC_GW_PRINCIPAL_ID
        read -r CC_GW_TENANT_ID
        read -r CC_GW_REGION
        read -r CC_SG_PRINCIPAL_ID
        read -r CC_SG_TENANT_ID
        read -r CC_SG_REGION
        read -r CC_CONN_STATUS
        read -r CC_CONN_STATUS_ERROR
        read -r CC_RUNTIME_URL
        read -r CC_RUNTIME_HOST
    } <<<"$scalars"
    CC_SG_GATEWAY_CONNECTIONS_JSON="$("$py" -c 'import json,sys; print(json.dumps(json.loads(sys.stdin.read()).get("sg_gc", [])))' <<<"$parsed" | tr -d '\r')"

    return 0
}

# Preflight: assert the wiring is in a state that will actually deliver
# Bearer auth to the in-sandbox runtime URL call. Each check prints the
# exact remediation command to stderr and adds an entry to CC_PREFLIGHT_ERRORS.
# Caller decides whether to exit non-zero (use `cc_preflight_failed` to test).
#
# Requires cc_resolve_all to have been called first.
cc_preflight() {
    CC_PREFLIGHT_ERRORS=()
    local py; py="$(_cc_py)" || return 1
    local fail="    ✗"
    local pass="    ✓"

    # Required scalars present?
    if [[ -z "${CC_GW_PRINCIPAL_ID:-}" ]]; then
        echo "$fail gateway '$ACA_CONNECTOR_GATEWAY' has no SystemAssigned managed identity." >&2
        echo "      fix: re-run 'azd provision' (or 'bash setup/setup.sh')" >&2
        CC_PREFLIGHT_ERRORS+=("gateway missing MI")
    else
        echo "$pass gateway MI present (principalId=$CC_GW_PRINCIPAL_ID)" >&2
    fi
    if [[ -z "${CC_SG_PRINCIPAL_ID:-}" ]]; then
        echo "$fail sandbox group '$ACA_SANDBOX_GROUP' has no SystemAssigned managed identity." >&2
        echo "      fix: aca sandboxgroup identity assign --name '$ACA_SANDBOX_GROUP' --system-assigned" >&2
        CC_PREFLIGHT_ERRORS+=("SG missing MI")
    else
        echo "$pass SG MI present (principalId=$CC_SG_PRINCIPAL_ID)" >&2
    fi

    # Connection status.
    if [[ "$CC_CONN_STATUS" != "Connected" ]]; then
        echo "$fail connection '$ACA_CONNECTOR_CONNECTION' status is '${CC_CONN_STATUS:-?}' (expected Connected)." >&2
        if [[ -n "${CC_CONN_STATUS_ERROR:-}" ]]; then
            echo "      reason: $CC_CONN_STATUS_ERROR" >&2
        fi
        echo "      fix: OAuth credential may have expired. Re-run 'azd provision' (or 'bash setup/setup.sh') to re-consent." >&2
        CC_PREFLIGHT_ERRORS+=("connection not Connected")
    else
        echo "$pass connection status: Connected" >&2
    fi

    # Runtime URL present + parseable.
    if [[ -z "${CC_RUNTIME_URL:-}" || -z "${CC_RUNTIME_HOST:-}" ]]; then
        echo "$fail connection '$ACA_CONNECTOR_CONNECTION' has no connectionRuntimeUrl (control plane hasn't minted it yet)." >&2
        echo "      fix: wait ~30s and retry; if persistent, re-run setup/setup.sh." >&2
        CC_PREFLIGHT_ERRORS+=("runtime URL missing")
    else
        echo "$pass runtime URL present ($CC_RUNTIME_HOST)" >&2
    fi

    # ACL: gateway-acl must point at the CURRENT gateway MI.
    local acls_url="https://management.azure.com/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.Web/connectorGateways/$ACA_CONNECTOR_GATEWAY/connections/$ACA_CONNECTOR_CONNECTION/accessPolicies?api-version=$CC_API_VERSION"
    local acls_json
    acls_json="$(cc_az_rest_retry --method GET --url "$acls_url" 2>/dev/null || echo '{"value":[]}')"

    local acl_check
    acl_check="$("$py" - <<'PYEOF' "$acls_json" "${CC_GW_PRINCIPAL_ID:-}" "${CC_SG_PRINCIPAL_ID:-}" | tr -d '\r'
import json, sys
acls = json.loads(sys.argv[1] or "{}").get("value", [])
gw_expected, sg_expected = sys.argv[2], sys.argv[3]
by_name = {a.get("name"): (((a.get("properties") or {}).get("principal") or {}).get("identity") or {}).get("objectId") or "" for a in acls if isinstance(a, dict)}
out = {
    "gw_acl_obj": by_name.get("gateway-acl", ""),
    "sb_acl_obj": by_name.get("sandbox-acl", ""),
    "gw_match": (by_name.get("gateway-acl", "").lower() == gw_expected.lower()) and bool(gw_expected),
    "sb_match": (by_name.get("sandbox-acl", "").lower() == sg_expected.lower()) and bool(sg_expected),
    "names": list(by_name.keys()),
}
print(json.dumps(out))
PYEOF
)"
    local gw_acl_obj sb_acl_obj gw_match sb_match acl_names
    gw_acl_obj="$("$py" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("gw_acl_obj",""))' <<<"$acl_check" | tr -d '\r')"
    sb_acl_obj="$("$py" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("sb_acl_obj",""))' <<<"$acl_check" | tr -d '\r')"
    gw_match="$("$py" -c   'import json,sys; print("1" if json.loads(sys.stdin.read()).get("gw_match") else "0")' <<<"$acl_check" | tr -d '\r')"
    sb_match="$("$py" -c   'import json,sys; print("1" if json.loads(sys.stdin.read()).get("sb_match") else "0")' <<<"$acl_check" | tr -d '\r')"
    acl_names="$("$py" -c  'import json,sys; print(",".join(json.loads(sys.stdin.read()).get("names",[])))' <<<"$acl_check" | tr -d '\r')"

    if [[ "$gw_match" != "1" ]]; then
        if [[ -z "$gw_acl_obj" ]]; then
            echo "$fail no 'gateway-acl' on connection '$ACA_CONNECTOR_CONNECTION' (found: ${acl_names:-none})." >&2
        else
            echo "$fail 'gateway-acl' on connection points at stale principal '$gw_acl_obj' (current gateway MI is '$CC_GW_PRINCIPAL_ID')." >&2
        fi
        echo "      fix: re-run 'azd provision' (or 'bash setup/setup.sh') — it will repair stale ACLs." >&2
        CC_PREFLIGHT_ERRORS+=("gateway-acl mismatch")
    else
        echo "$pass gateway-acl points at current gateway MI" >&2
    fi
    if [[ "$sb_match" != "1" ]]; then
        if [[ -z "$sb_acl_obj" ]]; then
            echo "$fail no 'sandbox-acl' on connection '$ACA_CONNECTOR_CONNECTION' (found: ${acl_names:-none})." >&2
        else
            echo "$fail 'sandbox-acl' on connection points at stale principal '$sb_acl_obj' (current SG MI is '$CC_SG_PRINCIPAL_ID')." >&2
        fi
        echo "      fix: re-run 'azd provision' (or 'bash setup/setup.sh') — it will repair stale ACLs." >&2
        CC_PREFLIGHT_ERRORS+=("sandbox-acl mismatch")
    else
        echo "$pass sandbox-acl points at current SG MI" >&2
    fi

    # SG-level gatewayConnections[] entry: must contain our resourceId AND
    # have its connectionRuntimeUrl equal the connection's CURRENT runtime
    # URL AND have authentication.type == SystemAssignedManagedIdentity.
    local sg_check
    sg_check="$("$py" - <<'PYEOF' "$CC_SG_GATEWAY_CONNECTIONS_JSON" "$CC_CONN_RESOURCE_ID" "${CC_RUNTIME_URL:-}" | tr -d '\r'
import json, sys
entries = json.loads(sys.argv[1] or "[]")
rid_lower = sys.argv[2].lower()
expected_url = sys.argv[3].rstrip("/")
found_entry = None
for e in entries:
    if isinstance(e, dict) and isinstance(e.get("resourceId"), str) and e["resourceId"].lower() == rid_lower:
        found_entry = e
        break
out = {"present": found_entry is not None}
if found_entry is not None:
    actual_url = (found_entry.get("connectionRuntimeUrl") or "").rstrip("/")
    auth_type = (((found_entry.get("authentication") or {}).get("type")) or "")
    out["url_match"]  = (actual_url == expected_url) and bool(expected_url)
    out["actual_url"] = actual_url
    out["auth_ok"]    = (auth_type == "SystemAssignedManagedIdentity")
    out["auth_type"]  = auth_type
print(json.dumps(out))
PYEOF
)"
    local sg_present sg_url_match sg_actual_url sg_auth_ok sg_auth_type
    sg_present="$("$py"     -c 'import json,sys; print("1" if json.loads(sys.stdin.read()).get("present") else "0")' <<<"$sg_check" | tr -d '\r')"
    sg_url_match="$("$py"   -c 'import json,sys; print("1" if json.loads(sys.stdin.read()).get("url_match") else "0")' <<<"$sg_check" | tr -d '\r')"
    sg_actual_url="$("$py"  -c 'import json,sys; print(json.loads(sys.stdin.read()).get("actual_url",""))' <<<"$sg_check" | tr -d '\r')"
    sg_auth_ok="$("$py"     -c 'import json,sys; print("1" if json.loads(sys.stdin.read()).get("auth_ok") else "0")' <<<"$sg_check" | tr -d '\r')"
    sg_auth_type="$("$py"   -c 'import json,sys; print(json.loads(sys.stdin.read()).get("auth_type",""))' <<<"$sg_check" | tr -d '\r')"

    if [[ "$sg_present" != "1" ]]; then
        echo "$fail sandbox group '$ACA_SANDBOX_GROUP' has no gatewayConnections[] entry for this connection." >&2
        echo "      fix: re-run 'azd provision' (or 'bash setup/setup.sh') — it will PATCH the SG." >&2
        CC_PREFLIGHT_ERRORS+=("SG gatewayConnections missing entry")
    elif [[ "$sg_url_match" != "1" ]]; then
        echo "$fail sandbox group's gatewayConnections[] entry has STALE connectionRuntimeUrl:" >&2
        echo "         have:    $sg_actual_url" >&2
        echo "         expected:$CC_RUNTIME_URL" >&2
        echo "      (the connection was recreated since the SG was wired; THIS is the most common cause of 401 missing-authorization-header.)" >&2
        echo "      fix: re-run 'azd provision' (or 'bash setup/setup.sh') — it will re-PATCH the SG with the current URL." >&2
        CC_PREFLIGHT_ERRORS+=("SG gatewayConnections stale runtime URL")
    elif [[ "$sg_auth_ok" != "1" ]]; then
        echo "$fail sandbox group's gatewayConnections[] entry has wrong authentication.type='${sg_auth_type:-missing}' (expected 'SystemAssignedManagedIdentity')." >&2
        echo "      fix: re-run 'azd provision' (or 'bash setup/setup.sh') — it will re-PATCH the SG." >&2
        CC_PREFLIGHT_ERRORS+=("SG gatewayConnections wrong auth type")
    else
        echo "$pass SG gatewayConnections[] entry matches current runtime URL + auth type" >&2
    fi
}

# Test whether preflight had any failures. Returns 0 if errors, 1 if clean.
# Usage: if cc_preflight_failed; then exit 1; fi
cc_preflight_failed() {
    [[ "${#CC_PREFLIGHT_ERRORS[@]}" -gt 0 ]]
}

# Idempotent ACL writer that REPAIRS stale ACLs (vs. setup.sh's old
# `ensure_acl` which treated `Exists/Conflict` as "skip" and would leave
# a stale objectId in place after a gateway/SG MI rotation).
#
# Strategy: GET the ACL first. If its objectId matches, no-op. Otherwise
# PUT — and if the PUT is rejected with Exists/Conflict, DELETE + PUT.
#
# Usage: cc_ensure_acl_current <acl_name> <principal_id> <tenant_id> <region>
cc_ensure_acl_current() {
    local _name="$1" _principal="$2" _tenant="$3" _region="$4"
    # Strip CRs from caller-supplied principal (it came from `az rest -o tsv`
    # on Windows which emits \r\n line endings).
    _principal="${_principal%$'\r'}"
    _tenant="${_tenant%$'\r'}"
    _region="${_region%$'\r'}"
    local acl_url="https://management.azure.com/subscriptions/$SUB/resourceGroups/$ACA_RESOURCE_GROUP/providers/Microsoft.Web/connectorGateways/$ACA_CONNECTOR_GATEWAY/connections/$ACA_CONNECTOR_CONNECTION/accessPolicies/$_name?api-version=$CC_API_VERSION"
    local current
    current="$(cc_az_rest_retry --method GET --url "$acl_url" --query "properties.principal.identity.objectId" -o tsv 2>/dev/null | tr -d '\r' || true)"
    if [[ -n "$current" && "${current,,}" == "${_principal,,}" ]]; then
        echo "    access policy '$_name' already current (objectId=$current)" >&2
        return 0
    fi
    if [[ -n "$current" ]]; then
        echo "    access policy '$_name' is stale (have=$current, want=$_principal) — replacing..." >&2
        cc_az_rest_retry --method DELETE --url "$acl_url" >/dev/null 2>&1 || true
    fi
    local body_file
    body_file="$(mktemp -t cc-acl-XXXXXX.json 2>/dev/null || mktemp)"
    cat > "$body_file" <<EOF
{"location":"$_region","properties":{"principal":{"type":"ActiveDirectory","identity":{"objectId":"$_principal","tenantId":"$_tenant"}}}}
EOF
    local body_path="$body_file"
    if command -v cygpath >/dev/null 2>&1; then body_path="$(cygpath -w "$body_file")"; fi
    local err
    if err="$(cc_az_rest_retry --method PUT --url "$acl_url" --headers "Content-Type=application/json" --body "@$body_path" 2>&1 >/dev/null)"; then
        echo "    access policy '$_name' applied (objectId=$_principal)" >&2
        rm -f "$body_file"
        return 0
    fi
    rm -f "$body_file"
    echo "error: PUT access policy '$_name' failed:" >&2
    echo "$err" >&2
    return 1
}

# Strip deprecated keys from a .env file (in-place). Used at setup-time
# to evict cached derived values that would otherwise drift.
cc_strip_env_keys() {
    local env_file="$1"
    [[ -f "$env_file" ]] || return 0
    local k tmp
    tmp="$env_file.tmp.$$"
    cp "$env_file" "$tmp"
    for k in "${CC_DEPRECATED_ENV_KEYS[@]}"; do
        if grep -q "^${k}=" "$tmp" 2>/dev/null; then
            grep -v "^${k}=" "$tmp" > "$tmp.2" || true
            mv "$tmp.2" "$tmp"
            echo "    .env: stripped deprecated key '$k' (now re-resolved from ARM)" >&2
        fi
    done
    mv "$tmp" "$env_file"
}
