#!/usr/bin/env bash
# Email -> sandbox -> Copilot-composed reply scenario driver (aca + az rest).
#
# Mirrors ../python/run.py. End-to-end round-trip:
#   1. Boot a sandbox on the `copilot` disk image (ships python3 + the
#      GitHub Copilot CLI pre-installed at /usr/local/bin/copilot). The
#      sandbox is created with a gatewayConnections[] entry pointing at
#      the Office 365 connection — the platform uses this to inject
#      Bearer auth on every outbound call to the connection's runtime
#      URL automatically.
#   2. Resolve a GitHub token for the Copilot CLI (operator env →
#      `gh auth token` → interactive `read -s` prompt). The token is
#      embedded briefly in a host temp launcher (umask 077, deleted
#      right after upload) and in /app/launch.sh inside the sandbox
#      (removed once the listener is up — from then on the token
#      lives only in the listener process's env, and is destroyed
#      with the sandbox).
#   3. Upload sandbox-app/server.py and start it on :5000 with the env vars
#      it needs to call SendMailV2 + invoke the Copilot CLI.
#   4. Lock down egress: Deny + host-Allow for the GitHub Copilot CLI
#      host families. The connection runtime URL host is mediated by
#      the platform's gatewayConnections-aware proxy (which also
#      injects Bearer auth) — no host-Allow rule and no Transform rule
#      required for it.
#   5. Smoke-test the runtime URL from inside the sandbox to confirm
#      the declarative wiring (SG-level gatewayConnections + sandbox-
#      level gatewayConnections) actually delivers auth.
#   6. One-shot `copilot -p 'ready'` round trip to confirm the token works.
#   7. Add port 5000 via data-plane POST /ports/add with
#      entraId.objectIds=[gateway MI] (+ tenantIds + your email so you
#      can hit /healthz from a browser) AND activationMode=OnDemand so
#      the proxy RESUMES the sandbox if it's suspended when the
#      gateway's webhook POST arrives.
#   8. PUT a trigger config: OnNewEmailV3, folderPath=Inbox,
#      subjectFilter=Feedback, MSI callback auth, recurrence Minute/1.
#   9. Print instructions and wait. Each "Feedback" email -> listener
#      hands the body to `copilot -p` -> reply via SendMailV2.
#  10. On Enter: tear down trigger -> port -> sandbox.
#
# Why we PUT the sandbox via `az rest` instead of `aca sandbox create`:
#   The `aca` CLI does not yet expose `--gateway-connection` on
#   `sandbox create`. We hit the dataplane PUT directly so we can pass
#   the gatewayConnections[] field; all subsequent operations
#   (exec, fs write, delete) still go through `aca`.

set -euo pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# On Windows, this script must run from Git Bash (MSYS) - NOT WSL. WSL bash
# has its own Linux filesystem, so JSON bodies we write under /tmp and pass
# to the Windows-native `az.cmd` as `@<file>` can't be read by az - it ships
# the literal string `@/tmp/...` to ARM, which then 400s on "'@' is an
# invalid start of a value". Detect WSL and bail with a clear message.
if grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null \
       || [[ -n "${WSL_DISTRO_NAME:-}" || -n "${WSL_INTEROP:-}" ]]; then
    cat >&2 <<'WSLERR'
error: this script is running under WSL bash, which cannot share temp files
       with the Windows-native `az` CLI it invokes.

       On Windows, run the scenario via the Git Bash wrapper instead:

           feedback-analyzer\run.cmd

       (Same command works from cmd or PowerShell. The wrapper finds
       Git Bash automatically.)
WSLERR
    exit 1
fi

API_VERSION="2026-05-01-preview"
DATAPLANE_API_VERSION="2026-02-01-preview"
DATAPLANE_RESOURCE="https://dynamicsessions.io"
PORT=5000
SUBJECT_FILTER="${ACA_TRIGGER_SUBJECT_FILTER:-Feedback}"
CONFIG_NAME="feedback-analyzer-demo"

# ----- load .env ---------------------------------------------------------
here="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$here/sandbox-app" && pwd)"
dir="$here"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do
    dir="$(dirname "$dir")"
done
[[ -f "$dir/.env" ]] || { echo "error: .env not found - run scenario setup first (bash ../setup/setup.sh)" >&2; exit 1; }
set -a; . "$dir/.env"; set +a

REQUIRED=(
    AZURE_SUBSCRIPTION_ID ACA_RESOURCE_GROUP ACA_SANDBOX_GROUP
    ACA_SANDBOXGROUP_REGION
    ACA_CONNECTOR_GATEWAY ACA_CONNECTOR_CONNECTION
    ACA_CONNECTOR_GATEWAY_PRINCIPAL_ID
    ACA_CONNECTOR_CONNECTION_RUNTIME_URL
)
for v in "${REQUIRED[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        echo "error: missing env var '$v' in .env." >&2
        echo "       Run scenario setup first: bash ../setup/setup.sh" >&2
        exit 1
    fi
done

SUB="$AZURE_SUBSCRIPTION_ID"
RG="$ACA_RESOURCE_GROUP"
SG="$ACA_SANDBOX_GROUP"
REGION="$ACA_SANDBOXGROUP_REGION"
GW="$ACA_CONNECTOR_GATEWAY"
CONN="$ACA_CONNECTOR_CONNECTION"
GW_PRINCIPAL="$ACA_CONNECTOR_GATEWAY_PRINCIPAL_ID"
GW_TENANT="${ACA_CONNECTOR_GATEWAY_TENANT_ID:-}"
RUNTIME_URL="${ACA_CONNECTOR_CONNECTION_RUNTIME_URL%/}"
USER_EMAIL="${ACA_USER_EMAIL:-}"
TRIAGE_TO="${TRIAGE_RECIPIENT:-$USER_EMAIL}"
if [[ -z "$TRIAGE_TO" || "$TRIAGE_TO" != *"@"* ]]; then
    echo "error: TRIAGE_RECIPIENT not set and ACA_USER_EMAIL is empty." >&2
    exit 1
fi

ARM_BASE="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Web/connectorGateways/$GW"
DP_BASE="https://management.${REGION}.azuredevcompute.io/subscriptions/$SUB/resourceGroups/$RG/sandboxGroups/$SG/sandboxes"

# Resolve the Python interpreter to use for host-side JSON/URL parsing.
# git-bash on Windows normally has `python` (not `python3`); macOS / most
# Linux distros ship `python3`. The Windows "python3" shim from the
# Microsoft Store is a no-op stub that prints an install nag and exits 0
# — skip it by also verifying that `import sys` actually runs.
_PY=""
for _cand in python3 python; do
    if command -v "$_cand" >/dev/null 2>&1; then
        if "$_cand" -c 'import sys' >/dev/null 2>&1; then
            _PY="$_cand"
            break
        fi
    fi
done
if [[ -z "$_PY" ]]; then
    echo "error: need Python 3.7+ on PATH (tried 'python3' and 'python')." >&2
    exit 1
fi

# python helper here is just for URL parsing; the sandbox uses its own python3.
host_from_url() {
    "$_PY" - <<EOF "$1"
import sys, urllib.parse
print(urllib.parse.urlparse(sys.argv[1]).hostname or "")
EOF
}
RUNTIME_HOST="$(host_from_url "$RUNTIME_URL")"
[[ -n "$RUNTIME_HOST" ]] || { echo "error: could not parse host from ACA_CONNECTOR_CONNECTION_RUNTIME_URL=$RUNTIME_URL" >&2; exit 1; }

TMPDIR_S="${TMPDIR:-/tmp}"
TMPFILES=()
mktmp() {
    local f
    f="$(mktemp "$TMPDIR_S/aca-trig-scn-XXXXXX.json")"
    TMPFILES+=("$f")
    printf '%s' "$f"
}

to_native() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi
}

SANDBOX_ID=""
PORT_ADDED=0
TRIGGER_CREATED=0
RUN_ID="$("$_PY" -c 'import uuid;print(uuid.uuid4().hex[:8])')"

cleanup() {
    if [[ "$TRIGGER_CREATED" == "1" ]]; then
        echo "==> DELETE trigger config '$CONFIG_NAME'"
        az rest --method DELETE \
            --url "$ARM_BASE/triggerConfigs/$CONFIG_NAME?api-version=$API_VERSION" \
            >/dev/null 2>&1 || echo "    warning: trigger delete failed"
    fi
    if [[ -n "$SANDBOX_ID" && "$PORT_ADDED" == "1" ]]; then
        echo "==> ports/remove (data plane) :$PORT"
        local body; body="$(mktmp)"
        echo "{\"port\":$PORT}" > "$body"
        az rest --method POST \
            --url "$DP_BASE/$SANDBOX_ID/ports/remove?api-version=$DATAPLANE_API_VERSION" \
            --resource "$DATAPLANE_RESOURCE" \
            --headers "Content-Type=application/json" \
            --body "@$(to_native "$body")" \
            >/dev/null 2>&1 || echo "    warning: port remove failed"
    fi
    if [[ -n "$SANDBOX_ID" ]]; then
        echo "==> aca sandbox delete --id $SANDBOX_ID"
        aca sandbox delete --group "$SG" --id "$SANDBOX_ID" --yes \
            >/dev/null 2>&1 || echo "    warning: sandbox delete failed"
    fi
    rm -f "${TMPFILES[@]:-}" 2>/dev/null || true
}
trap cleanup EXIT

# ----- 0. Resolve GitHub token for Copilot CLI ---------------------------
# Resolution order (first non-empty wins) - mirrors python/run.py.
echo "==> Resolving GitHub token for Copilot CLI..."
COPILOT_TOKEN=""
for v in COPILOT_GITHUB_TOKEN GH_TOKEN GITHUB_TOKEN; do
    val="${!v:-}"
    if [[ -n "$val" ]]; then
        COPILOT_TOKEN="$val"
        echo "    using \$$v from operator env"
        break
    fi
done
if [[ -z "$COPILOT_TOKEN" ]] && command -v gh >/dev/null 2>&1; then
    if tok="$(gh auth token 2>/dev/null)" && [[ -n "$tok" ]]; then
        COPILOT_TOKEN="$tok"
        echo "    using \`gh auth token\` from local GitHub CLI"
    fi
fi
if [[ -z "$COPILOT_TOKEN" ]]; then
    if [[ ! -t 0 ]]; then
        echo "error: no GitHub token configured and stdin is not a TTY." >&2
        echo "       Set COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN, or run \`gh auth login\` first." >&2
        exit 1
    fi
    echo
    echo "    The sandbox's Copilot CLI needs a GitHub token (OAuth ghu_/gho_"
    echo "    or fine-grained PAT with the 'Copilot Requests' permission)."
    echo "    Token input is hidden and never logged."
    read -r -s -p "    Paste GitHub token: " COPILOT_TOKEN
    echo
fi
[[ -n "$COPILOT_TOKEN" ]] || { echo "error: no GitHub token provided." >&2; exit 1; }

# ----- 1. Sandbox --------------------------------------------------------
# Dataplane PUT (no sandbox id in URL, no api-version, no apiVersion
# param — Cascade shape) so we can pass gatewayConnections[]. The
# per-sandbox entry mirrors the SG-level entry shape ({resourceId,
# connectionRuntimeUrl, authentication.type=SystemAssignedManagedIdentity})
# so the platform's connector-gateway-aware proxy injects Bearer auth
# automatically on every outbound call to the runtime URL.
echo "==> Creating sandbox in '$SG' (labels.run=$RUN_ID) with gatewayConnections=[$CONN]..."
CONNECTION_RESOURCE_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Web/connectorGateways/$GW/connections/$CONN"
SANDBOX_BODY="$(mktmp)"
"$_PY" - "$CONNECTION_RESOURCE_ID" "$RUNTIME_URL" "$RUN_ID" > "$SANDBOX_BODY" <<'PYEOF'
import json, sys
resource_id, runtime_url, run_id = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
    "sourcesRef": {"diskImage": {"name": "copilot", "isPublic": True}},
    "vmmType": "CloudHypervisor",
    "resources": {"cpu": "2000m", "memory": "4096Mi", "disk": "20480Mi"},
    "gatewayConnections": [{
        "resourceId": resource_id,
        "connectionRuntimeUrl": runtime_url,
        "authentication": {"type": "SystemAssignedManagedIdentity"},
    }],
    "labels": {"sample": "connector-trigger-email", "run": run_id},
}))
PYEOF
SANDBOX_BASE="https://management.${REGION}.azuredevcompute.io/subscriptions/$SUB/resourceGroups/$RG/sandboxGroups/$SG/sandboxes"
CREATE_RESP="$(az rest --method PUT \
    --url "$SANDBOX_BASE" \
    --resource "$DATAPLANE_RESOURCE" \
    --headers "Content-Type=application/json" \
    --body "@$(to_native "$SANDBOX_BODY")")"
SANDBOX_ID="$("$_PY" - "$CREATE_RESP" <<'PYEOF'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
sid = data.get("id") or data.get("sandboxId") or data.get("name") or ""
if isinstance(sid, str) and "/" in sid:
    sid = sid.rsplit("/", 1)[-1]
print(sid or "")
PYEOF
)"
[[ -n "$SANDBOX_ID" ]] || { echo "error: dataplane sandbox PUT returned no id" >&2; echo "$CREATE_RESP" >&2; exit 1; }
echo "    sandbox: $SANDBOX_ID"

# ----- 2. Verify copilot CLI is present (the disk image ships it) -------
echo "==> Verifying copilot CLI is present..."
aca sandbox exec --group "$SG" --id "$SANDBOX_ID" -c \
    "command -v copilot && copilot --version" >/dev/null

# ----- 3. Upload + start -------------------------------------------------
echo "==> Uploading sandbox-app/server.py into /app..."
aca sandbox exec --group "$SG" --id "$SANDBOX_ID" -c "mkdir -p /app" >/dev/null
aca sandbox fs write --group "$SG" --id "$SANDBOX_ID" \
    --path /app/server.py --file "$(to_native "$APP_DIR/server.py")" >/dev/null

echo "==> Starting listener on :$PORT (setsid, logs at /tmp/listener.log)..."
# Write a small launcher.sh into the sandbox to dodge nested-shell quoting
# around env values, and use `setsid ... </dev/null` to fully detach the
# python process from the exec session (nohup alone is not enough — the
# sandbox reaps the exec process group when the session ends).
#
# Token-hygiene: create the host temp file with `umask 077`, upload, then
# delete it immediately (don't wait for the EXIT trap). The same file
# inside the sandbox at /app/launch.sh is removed once the listener is up
# (the secret lives only in the listener process's env from then on).
(
    umask 077
    LAUNCHER="$TMPDIR_S/aca-trig-launch-$$-$RANDOM.sh"
    # Belt-and-suspenders: even if `aca sandbox fs write` fails under
    # `set -e` and skips the explicit `rm` below, the trap below
    # guarantees the on-host token file is removed when this subshell
    # exits (success or failure).
    trap 'rm -f "$LAUNCHER"' EXIT
    cat > "$LAUNCHER" <<EOF
#!/bin/bash
set -u
export PORT=$(printf '%q' "$PORT")
export O365_RUNTIME_URL=$(printf '%q' "$RUNTIME_URL")
export TRIAGE_RECIPIENT=$(printf '%q' "$TRIAGE_TO")
export COPILOT_GITHUB_TOKEN=$(printf '%q' "$COPILOT_TOKEN")
pkill -f 'python3 /app/server.py' 2>/dev/null || true
sleep 1
rm -f /tmp/listener.log /tmp/listener.pid
setsid nohup python3 /app/server.py > /tmp/listener.log 2>&1 < /dev/null &
disown || true
echo \$! > /tmp/listener.pid
EOF
    aca sandbox fs write --group "$SG" --id "$SANDBOX_ID" \
        --path /app/launch.sh --file "$(to_native "$LAUNCHER")" >/dev/null
    rm -f "$LAUNCHER"
)
aca sandbox exec --group "$SG" --id "$SANDBOX_ID" -c "bash /app/launch.sh" >/dev/null

for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    code="$(aca sandbox exec --group "$SG" --id "$SANDBOX_ID" -c \
        "curl -fsS -o /dev/null -w '%{http_code}' http://localhost:$PORT/healthz || true" \
        2>/dev/null | tail -n1 | tr -d '[:space:]' || true)"
    [[ "$code" == "200" ]] && break
    sleep 2
done
[[ "$code" == "200" ]] || { echo "error: listener never returned 200 (last=$code)" >&2; exit 1; }
# Remove the in-sandbox launcher script now that the listener has the token
# in its process env. The sandbox itself will be deleted on EXIT regardless.
aca sandbox exec --group "$SG" --id "$SANDBOX_ID" -c "rm -f /app/launch.sh" \
    >/dev/null 2>&1 || true
echo "    listener is up"

# ----- 4. Egress: Deny + GitHub host-Allow -------------------------------
# Connection runtime URL host (RUNTIME_HOST) is NOT in the host-Allow
# list because the platform's gatewayConnections-aware proxy mediates
# calls to it independently of the egress policy AND injects Bearer
# auth on the platform path. Verified live: Deny + no runtime allow +
# no Transform still returns HTTP 200 for the connection runtime URL.
echo "==> Locking down egress: Deny + GitHub host-allows (runtime URL $RUNTIME_HOST mediated by platform)..."
EGRESS_BODY="$(mktmp)"
cat > "$EGRESS_BODY" <<EOF
{
  "defaultAction": "Deny",
  "hostRules": [
    { "pattern": "github.com",              "action": "Allow" },
    { "pattern": "*.github.com",            "action": "Allow" },
    { "pattern": "*.githubusercontent.com", "action": "Allow" },
    { "pattern": "gh.io",                   "action": "Allow" },
    { "pattern": "*.github.io",             "action": "Allow" },
    { "pattern": "githubcopilot.com",       "action": "Allow" },
    { "pattern": "*.githubcopilot.com",     "action": "Allow" }
  ]
}
EOF
az rest --method POST \
    --url "$DP_BASE/$SANDBOX_ID/egresspolicy?api-version=$DATAPLANE_API_VERSION" \
    --resource "$DATAPLANE_RESOURCE" \
    --headers "Content-Type=application/json" \
    --body "@$(to_native "$EGRESS_BODY")" \
    >/dev/null

echo "==> Egress smoke test: GET $RUNTIME_HOST/v2/Mail?folderPath=Inbox&top=1..."
TEST_URL="$RUNTIME_URL/v2/Mail?folderPath=Inbox&top=1"
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18; do
    code="$(aca sandbox exec --group "$SG" --id "$SANDBOX_ID" -c \
        "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 $(printf '%q' "$TEST_URL") || true" \
        2>/dev/null | tail -n1 | tr -d '[:space:]' || true)"
    if [[ "$code" == "200" ]]; then ok=1; break; fi
    sleep 5
done
[[ "$ok" == "1" ]] || { echo "error: egress smoke test failed (last http_code=${code:-?})." >&2; \
    echo "       Check: (a) sandbox-acl exists on the connection, (b) the sandbox" >&2; \
    echo "       group has a SystemAssigned MI and a gatewayConnections[] entry" >&2; \
    echo "       referencing this connection (set by setup.sh), (c) this sandbox" >&2; \
    echo "       was created with the same connection in its gatewayConnections" >&2; \
    echo "       list (handled by this script)." >&2; \
    exit 1; }
echo "    egress ok — runtime URL reachable + platform auth-injection working"

# ----- 5. Verify Copilot CLI auth (token round-trip) --------------------
echo "==> Verifying Copilot CLI auth (one-shot 'ready' probe)..."
# pipefail in the remote shell so a copilot failure (with output) doesn't
# get masked by the `tail -c 400` exit code.
REMOTE_PROBE="set -o pipefail; timeout 60 env COPILOT_GITHUB_TOKEN=$(printf '%q' "$COPILOT_TOKEN") copilot -p 'reply with the single word ready' -s --allow-all-tools 2>&1 | tail -c 400"
PROBE_OUT="$(aca sandbox exec --group "$SG" --id "$SANDBOX_ID" -c "$REMOTE_PROBE" 2>&1 | tr -d '\r' || true)"
if [[ -z "$PROBE_OUT" ]] || ! echo "$PROBE_OUT" | grep -qi 'ready'; then
    safe="${PROBE_OUT//$COPILOT_TOKEN/<redacted>}"
    echo "error: copilot CLI rejected the supplied token (no 'ready' in output)." >&2
    echo "       Accepted token types: OAuth (ghu_/gho_) and fine-grained PATs" >&2
    echo "       with 'Copilot Requests'. Classic ghp_ tokens are NOT supported." >&2
    echo "       Probe output:" >&2
    echo "$safe" | head -c 400 >&2; echo >&2
    exit 1
fi
echo "    copilot CLI auth ok"

# ----- 6. Port (data plane POST /ports/add with entraId.objectIds + activationMode) -
# `activationMode` is a *port-level* field — set it on the port so the
# proxy RESUMES the sandbox before forwarding the gateway's webhook
# POST (sandbox can scale to zero between emails). We use POST
# /ports/add (not PUT /ports) so the platform assigns the proxy URL
# for us; PUT /ports is a "replace existing view" call that requires
# the url already exist.
echo "==> add port $PORT (entraId.objectIds=[gateway MI]${GW_TENANT:+, tenantIds=[gateway tenant]}, activationMode=OnDemand)"
PORT_BODY="$(mktmp)"
"$_PY" - "$PORT" "$GW_PRINCIPAL" "$GW_TENANT" "$USER_EMAIL" > "$PORT_BODY" <<'PYEOF'
import json, sys
port = int(sys.argv[1])
gw_principal = sys.argv[2]
gw_tenant = sys.argv[3].strip()
user_email = sys.argv[4].strip()
entra = {"enabled": True, "objectIds": [gw_principal]}
if gw_tenant:
    entra["tenantIds"] = [gw_tenant]
if user_email and "@" in user_email:
    entra["emails"] = [user_email]
print(json.dumps({
    "port": port,
    "auth": {"entraId": entra},
    "activationMode": "OnDemand",
}))
PYEOF
PORT_RESP="$(az rest --method POST \
    --url "$DP_BASE/$SANDBOX_ID/ports/add?api-version=$DATAPLANE_API_VERSION" \
    --resource "$DATAPLANE_RESOURCE" \
    --headers "Content-Type=application/json" \
    --body "@$(to_native "$PORT_BODY")")"
PORT_ADDED=1
# Pass the response as an argv (not stdin) so the heredoc-as-script for `python3 -`
# doesn't collide with reading the JSON payload.
PORT_URL="$("$_PY" - "$PORT" "$PORT_RESP" <<'PYEOF'
import json, sys
port = int(sys.argv[1])
data = json.loads(sys.argv[2] or "{}")
ports = data.get("ports") if isinstance(data, dict) else data
if not isinstance(ports, list):
    ports = []
match = next((p for p in ports if isinstance(p, dict) and p.get("port") == port), None)
print((match or {}).get("url", "") or (data.get("url", "") if isinstance(data, dict) else ""))
PYEOF
)"
[[ -n "$PORT_URL" ]] || { echo "error: POST /ports/add returned no url" >&2; echo "$PORT_RESP" >&2; exit 1; }
CALLBACK_URL="${PORT_URL%/}/webhook"

# ----- 7. Trigger config -------------------------------------------------
echo "==> PUT trigger config '$CONFIG_NAME'..."
BODY_FILE="$(mktmp)"
# Build the trigger body with python3 so user-controlled values
# (SUBJECT_FILTER) are JSON-encoded safely rather than concatenated.
"$_PY" - "$CONN" "$SG" "$SANDBOX_ID" "$CALLBACK_URL" "$SUBJECT_FILTER" > "$BODY_FILE" <<'PYEOF'
import json, sys
conn, sg, sid, callback, subject_filter = sys.argv[1:6]
parameters = [{"name": "folderPath", "value": "Inbox"}]
if subject_filter:
    parameters.append({"name": "subjectFilter", "value": subject_filter})
body = {
    "properties": {
        "state": "Enabled",
        "connectionDetails": {
            "connectorName": "office365",
            "connectionName": conn,
        },
        "metadata": {
            "sandboxGroupName": sg,
            "sandboxId": sid,
            "recurrenceFrequency": "Minute",
            "recurrenceInterval": 1,
        },
        "notificationDetails": {
            "callbackUrl": callback,
            "httpMethod": "POST",
            "authentication": {
                "type": "ManagedServiceIdentity",
                "audience": "https://auth.adcproxy.io/",
            },
        },
        "operationName": "OnNewEmailV3",
        "parameters": parameters,
    }
}
print(json.dumps(body))
PYEOF

# Retry up to 3 times on transient 5xx (control plane occasionally returns
# Internal Server Error while propagating connector-gateway state).
STATE=""
for attempt in 1 2 3 4; do
    if STATE="$(az rest --method PUT \
        --url "$ARM_BASE/triggerConfigs/$CONFIG_NAME?api-version=$API_VERSION" \
        --headers "Content-Type=application/json" \
        --body "@$(to_native "$BODY_FILE")" \
        --query "properties.state" -o tsv 2>/dev/null)"; then
        break
    fi
    if [[ "$attempt" == "4" ]]; then
        echo "error: trigger PUT failed after retries" >&2
        az rest --method PUT \
            --url "$ARM_BASE/triggerConfigs/$CONFIG_NAME?api-version=$API_VERSION" \
            --headers "Content-Type=application/json" \
            --body "@$(to_native "$BODY_FILE")" >&2 || true
        exit 1
    fi
    sleep $((5 * attempt))
done
TRIGGER_CREATED=1

# ----- 8. Print + wait ---------------------------------------------------
echo
echo "========================================================================"
echo "Feedback-analyzer trigger is live"
echo "========================================================================"
echo "  trigger config:  $CONFIG_NAME (state=${STATE:-?})"
echo "  listener URL:    $PORT_URL  (healthz only — webhook is gateway-only)"
echo "  callback URL:    $CALLBACK_URL"
echo "  reply goes to:   $TRIAGE_TO"
echo
echo "To fire the trigger:"
echo "  1. Send yourself (or ${USER_EMAIL:-the consent user}) an email whose"
echo "     subject contains the word '$SUBJECT_FILTER' (case-insensitive)."
echo "  2. Wait ~1 minute — Office 365 trigger delivery is not instant."
echo "  3. Watch $TRIAGE_TO's inbox for a Copilot-composed reply with subject"
echo "     'Auto-ack: received your message'."
echo
echo "Listener logs (from another terminal):"
echo "  aca sandbox exec -g $RG --group $SG \\"
echo "    --id $SANDBOX_ID --command 'tail -f /tmp/listener.log'"
echo
echo "When done, press Enter here to tear everything down"
echo "(trigger -> port -> sandbox; gateway + connection are kept)."
echo "========================================================================"
read -r -p "Press Enter to continue... " _ || true
echo "    (cleanup runs on EXIT)"
