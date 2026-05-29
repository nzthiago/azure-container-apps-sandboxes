"""Email → sandbox → email reply scenario driver (Python).

End-to-end round-trip demo of "row 8 - Triggers, reacting to outside events":

  1. Boot a sandbox on the ``copilot`` disk image (ships ``python3`` +
     ``/usr/local/bin/copilot`` — the GitHub Copilot CLI). The sandbox
     is created with a ``gatewayConnections[]`` entry pointing at the
     Office 365 connection — the platform uses this to inject
     ``Authorization: Bearer <SG-MI-token>`` on every outbound call to
     the connection's runtime URL.
  2. Upload the listener (``server.py``, shared with the CLI flavor of
     this sample — see ``APP_DIR`` below) and start it on :5000 with the env vars
     it needs to call SendMailV2 + invoke the Copilot CLI.
  3. Lock down egress: ``default = Deny`` plus host-Allow rules for
     ``github.com`` / ``*.githubcopilot.com`` / ``gh.io`` etc. (so the
     Copilot CLI can log in + reach the model API). The connector
     runtime URL host is mediated by the platform's
     ``gatewayConnections``-aware proxy, so we **don't** need to allow
     it in the egress policy or set up a per-sandbox Transform rule —
     auth and traversal both happen on the platform path.
  4. Smoke-test the runtime URL from inside the sandbox
     (``GET {RUNTIME_URL}/v2/Mail?folderPath=Inbox&top=1``) — proves the
     declarative wiring (SG-level ``gatewayConnections`` + sandbox-level
     ``gatewayConnections``) actually delivers Bearer auth.
  5. **Inject the operator's GitHub token into the sandbox** so the
     pre-installed Copilot CLI can call the model API headlessly. We
     pick up a token from (in order) ``COPILOT_GITHUB_TOKEN`` /
     ``GH_TOKEN`` / ``GITHUB_TOKEN`` env vars, ``gh auth token`` from
     the local GitHub CLI, or an interactive ``getpass`` prompt.
     The token is exported as ``COPILOT_GITHUB_TOKEN`` in the listener
     process and verified with a tiny round-trip prompt before the
     trigger is registered.
  6. Add port :5000 with the gateway managed identity in
     ``entraId.objectIds`` so the connector gateway can reach the
     listener, AND set ``activationMode=OnDemand`` on the port so the
     proxy RESUMES the sandbox if it has scaled to zero when the
     webhook POST arrives.
  7. PUT a trigger config: ``OnNewEmailV3``, folder ``Inbox``,
     ``subjectFilter=Feedback``, callback auth ``ManagedServiceIdentity``
     against audience ``https://auth.adcproxy.io/``.
  8. Print instructions and wait. Each "Feedback" email triggers the
     listener, which invokes ``copilot -p ...`` to compose a warm
     acknowledgment, then sends it back via SendMailV2 on the **same**
     connection.
  9. On Enter: tear down trigger config → port → sandbox. The Copilot
     login session lives inside the sandbox and is destroyed with it
     (the GitHub OAuth grant remains on github.com until revoked).

The connector gateway, Office 365 connection, sandbox-group MI, both access
policies, the runtime URL, and the sandbox-group ``gatewayConnections[]``
wiring are provisioned once by
``python/samples/10-connectors-triggers/setup/setup.py``;
this script only owns the trigger config, sandbox, port, and egress policy.

Why we PUT the sandbox via ``az rest`` instead of the SDK:
  The published ``azure-containerapps-sandbox`` Python SDK does not yet
  expose ``gateway_connections`` on ``SandboxGroupClient.begin_create_sandbox``.
  We hit the dataplane directly with ``az rest`` for the create call so
  we can pass that field, then wrap the returned id with
  ``SandboxGroupClient.get_sandbox_client(sandbox_id)`` to get the typed
  ``SandboxClient`` for ``.exec()`` / ``.write_file()`` / ``.delete()``
  back. Once the SDK ships ``gateway_connections``, replace the create
  call with ``begin_create_sandbox(gateway_connections=[...]).result()``.

Notes:
  * Office 365 trigger delivery can lag by **several minutes** — this is
    normal. Don't kill the script if nothing happens immediately.
  * The trigger fires on emails arriving *after* it was created. Old
    inbox messages don't backfill.
  * The reply subject is ``Auto-ack: received your message`` —
    deliberately free of the word "Feedback" to avoid an infinite
    self-trigger loop.
  * The Copilot CLI uses your GitHub token via env var injection (the
    documented headless-auth path — see ``copilot help environment``).
    If you have ``gh auth login`` done locally we pick it up
    automatically; otherwise you'll be prompted once. The token never
    touches disk on the operator's host.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import (
    SandboxGroupClient,
    endpoint_for_region,
)

# Shared helpers (ARM re-resolution + preflight drift checks). Live in
# the setup tree so setup.py and run.py use the SAME code — there's no
# second copy in run.py to drift from setup.py. See
# ../setup/connector_common.py for the full rationale.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "setup"),
)
import connector_common as cc  # noqa: E402

API_VERSION = "2026-05-01-preview"
DATAPLANE_API_VERSION = "2026-02-01-preview"
# Token audience for the sandbox data plane (matches the SDK's
# DATA_PLANE_SCOPE = "https://dynamicsessions.io/.default").
DATAPLANE_RESOURCE = "https://dynamicsessions.io"
PORT = 5000
SCENARIO_DIR = Path(__file__).resolve().parent.parent
# The sandbox-app listener is identical for both language flavors of this
# sample (it runs INSIDE the sandbox), so it's kept as a single canonical
# copy in the CLI tree to avoid duplication. SCENARIO_DIR.parents[2] is
# the repo root (python/samples/<scenario> -> ../../.. -> repo root).
APP_DIR = (
    SCENARIO_DIR.parents[2]
    / "cli" / "samples" / "10-connectors-triggers"
    / "feedback-analyzer" / "sandbox-app"
)
if not (APP_DIR / "server.py").is_file():
    raise SystemExit(
        f"error: could not find shared sandbox-app at {APP_DIR}.\n"
        "       This Python flavor reuses the listener from the CLI tree.\n"
        "       Make sure cli/samples/10-connectors-triggers/feedback-analyzer/\n"
        "       sandbox-app/server.py exists alongside this Python tree."
    )
SUBJECT_FILTER = os.environ.get("ACA_TRIGGER_SUBJECT_FILTER", "Feedback")


# ---------- env loading ----------------------------------------------------

REQUIRED_ENV = (
    "AZURE_SUBSCRIPTION_ID", "ACA_RESOURCE_GROUP", "ACA_SANDBOX_GROUP",
    "ACA_SANDBOXGROUP_REGION",
    "ACA_CONNECTOR_GATEWAY", "ACA_CONNECTOR_CONNECTION",
)


def _load_env() -> None:
    for parent in Path(__file__).resolve().parents:
        env = parent / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            break
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(
            f"error: .env missing {missing}.\n"
            "       Run scenario setup first:\n"
            "         python ../setup/setup.py"
        )


# ---------- az helpers -----------------------------------------------------

def _az_rest(method: str, url: str, body: dict | None = None,
             resource: str | None = None,
             check: bool = True,
             retry_on_5xx: int = 0) -> tuple[int, str, str]:
    cmd = ["az", "rest", "--method", method, "--url", url]
    if resource:
        cmd += ["--resource", resource]
    tmp = None
    if body is not None:
        fd, tmp = tempfile.mkstemp(prefix="aca-trig-scn-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f)
        cmd += ["--body", f"@{tmp}"]
    try:
        attempt = 0
        while True:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               shell=sys.platform == "win32")
            if r.returncode == 0:
                return r.returncode, r.stdout, r.stderr
            is_5xx = ("Internal Server Error" in (r.stderr or "")
                      or "InternalServerError" in (r.stderr or "")
                      or "Bad Gateway" in (r.stderr or "")
                      or "Service Unavailable" in (r.stderr or "")
                      or "Gateway Timeout" in (r.stderr or ""))
            if is_5xx and attempt < retry_on_5xx:
                backoff = 5 * (attempt + 1)
                print(f"    transient 5xx on {method} {url.split('?')[0]} - "
                      f"retrying in {backoff}s (attempt {attempt + 1}/{retry_on_5xx})")
                time.sleep(backoff)
                attempt += 1
                continue
            if check:
                sys.exit(
                    f"error: az rest {method} {url} failed (exit={r.returncode}):\n"
                    f"{r.stderr.strip()[:800]}"
                )
            return r.returncode, r.stdout, r.stderr
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------- in-sandbox helpers --------------------------------------------

def _wait_in_sandbox(sandbox, url, timeout=60, log_path=None, pid_path=None):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        r = sandbox.exec(
            f"curl -fsS -o /dev/null -w '%{{http_code}}' {url} || true",
        )
        last = (r.stdout or "").strip()
        if last == "200":
            return
        time.sleep(2)
    # Best-effort diagnostics: PID alive? log contents?
    diag = []
    if pid_path:
        r = sandbox.exec(
            f"if [ -f {pid_path} ]; then "
            f"  pid=$(cat {pid_path}); "
            f"  if kill -0 \"$pid\" 2>/dev/null; then echo \"pid $pid alive\"; "
            f"  else echo \"pid $pid DEAD\"; fi; "
            f"else echo 'no pid file'; fi"
        )
        diag.append((r.stdout or "").strip() or "pid: ?")
    if log_path:
        r = sandbox.exec(
            f"if [ -f {log_path} ]; then tail -c 4000 {log_path}; "
            f"else echo '(no log file at {log_path})'; fi"
        )
        log = (r.stdout or "").strip() or "(empty log)"
        diag.append(f"log:\n{log}")
    raise RuntimeError(
        f"listener not ready after {timeout}s (last http_code={last!r})"
        + ("\n" + "\n".join(diag) if diag else "")
    )


def _smoke_test_gateway_auth_injection(sandbox, runtime_url, timeout=90):
    """End-to-end check that the gateway-connection auth-injection chain works.

    From INSIDE the sandbox, GET ``{runtime_url}/v2/Mail?folderPath=Inbox&top=1``
    with NO Authorization header. The platform's gatewayConnections-aware
    egress layer is supposed to:
      1. Detect that the destination host matches a sandbox
         ``gatewayConnections[]`` entry.
      2. Look up the SG-level entry by ``resourceId`` to get the auth
         type (SystemAssignedManagedIdentity).
      3. Fetch a Bearer token via the sandbox-group MI (the connection's
         send-side access policy lets it).
      4. Inject ``Authorization: Bearer ...`` and forward to Office 365.

    A 200 here proves the wiring landed correctly on BOTH the SG and the
    sandbox. A non-200 (typically 401) means the platform did NOT inject
    auth — usually because ``gatewayConnections[]`` is missing or stale
    in one of those two places (the post-PUT verification in
    ``_create_sandbox_with_gateway_connection`` and the preflight in
    ``setup.py``/``connector_common.preflight()`` should catch most cases
    pre-flight; this is the final live confirmation).

    It does NOT prove ``SendMailV2`` succeeds — that depends on Mail.Send
    permission on the connection, the recipient address, and the payload.
    Retries because ACL propagation can lag ~30-60s after the ACL PUT."""
    test_url = f"{runtime_url}/v2/Mail?folderPath=Inbox&top=1"
    cmd = (
        "curl -sS -o /dev/null -w '%{http_code}' "
        f"--max-time 15 {shlex.quote(test_url)} || true"
    )
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        r = sandbox.exec(cmd)
        last = (r.stdout or "").strip()
        if last == "200":
            return
        time.sleep(5)
    raise RuntimeError(
        f"gateway-connection auth-injection smoke test failed after {timeout}s "
        f"(last http_code={last!r}). The platform proxy did NOT inject a Bearer "
        "token on the call to the runtime URL. Wiring is broken in one of:\n"
        "  (a) sandbox-acl missing/stale on the connection\n"
        "      (preflight should have caught — re-run setup.py)\n"
        "  (b) sandbox-group has no SystemAssigned MI or its\n"
        "      gatewayConnections[] entry doesn't match this connection\n"
        "      (preflight should have caught — re-run setup.py)\n"
        "  (c) THIS sandbox was created without gatewayConnections[] in\n"
        "      its spec (post-PUT verification above should have caught)\n"
        "  (d) connection OAuth credential expired — re-run azd provision"
    )


# Backward-compat alias (older callers / docs may reference the old name).
_smoke_test_egress = _smoke_test_gateway_auth_injection


# ---------- copilot CLI authentication --------------------------------------

# The Copilot CLI installed on the `copilot` disk image needs a GitHub
# token to call the model API. Per `copilot help environment`, the CLI
# picks up COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN env vars (in
# order of precedence) — that's the documented headless-automation path
# and the one we use here.
#
# The interactive `copilot login` device-code flow requires an OS
# keychain (libsecret + DBus on Linux) to persist the token; the bare
# sandbox doesn't have one, so login completes but discards the token.
# Env-var injection avoids the keychain entirely.

_TOKEN_PROBE_CMD_TEMPLATE = (
    "timeout 60 env COPILOT_GITHUB_TOKEN={token} "
    "copilot -p 'reply with the single word ready' "
    "-s --allow-all-tools 2>&1 | tail -c 400"
)


def _resolve_copilot_token() -> str:
    """Get a GitHub token suitable for the Copilot CLI.

    Resolution order (first non-empty wins):

      1. ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN``
         env vars on the operator's machine (handy for CI / shared
         dev boxes).
      2. ``gh auth token`` from the local GitHub CLI (zero-friction
         for any operator already signed into ``gh``).
      3. Interactive ``getpass`` prompt (no echo, no logging).

    The token is passed into the sandbox via the listener launcher's
    env block and never written to a file on the operator's host.
    """
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        v = os.environ.get(name, "").strip()
        if v:
            print(f"    using ${name} from operator env")
            return v

    try:
        p = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=15,
        )
        tok = (p.stdout or "").strip()
        if p.returncode == 0 and tok:
            print("    using `gh auth token` from local GitHub CLI")
            return tok
    except FileNotFoundError:
        pass  # gh not installed on this machine
    except subprocess.TimeoutExpired:
        pass

    import getpass
    print()
    print("    The sandbox's Copilot CLI needs a GitHub token.")
    print("    Create a fine-grained PAT with the 'Copilot Requests'")
    print("    permission at https://github.com/settings/tokens?type=beta")
    print("    or use any OAuth/CLI token (ghu_, gho_, github_pat_).")
    print("    Token input is hidden and never logged.")
    tok = getpass.getpass("    Paste GitHub token: ").strip()
    if not tok:
        sys.exit(
            "error: no GitHub token provided. Re-run with COPILOT_GITHUB_TOKEN "
            "set, log into the gh CLI (`gh auth login`), or paste a token at "
            "the prompt."
        )
    return tok


def _verify_copilot_token(sandbox, token: str) -> None:
    """Confirm the token actually works by round-tripping one prompt
    through the Copilot CLI inside the sandbox."""
    cmd = _TOKEN_PROBE_CMD_TEMPLATE.format(token=shlex.quote(token))
    r = sandbox.exec(cmd)
    out = (r.stdout or "").strip()
    if r.exit_code != 0 or not out or "Error" in out[:60]:
        # Strip the token from any diagnostic before raising.
        safe_out = out.replace(token, "<redacted>") if token else out
        raise RuntimeError(
            "copilot CLI rejected the supplied token. The CLI accepts "
            "OAuth tokens (ghu_/gho_) and fine-grained PATs with the "
            "'Copilot Requests' permission. Classic ghp_ tokens are NOT "
            f"supported. Probe output:\n{safe_out[:400]}"
        )
    print(f"    copilot CLI is authenticated ({out[:60]!r})")


# ---------- data-plane helpers ---------------------------------------------

def _dp_url(endpoint: str, sub: str, rg: str, sg: str,
            sandbox_id: str, suffix: str) -> str:
    """Build a sandbox data-plane URL for ``az rest`` against the regional
    endpoint (e.g. ``https://management.westus2.azuredevcompute.io``).

    We hit the data plane directly (not via the SDK) for three payload
    shapes the typed SDK models don't currently express:
    ``gatewayConnections`` on sandbox create, ``entraId.objectIds`` on a
    port, and (legacy) Transform headers on egress policy. Everything
    else (exec, write_file, delete) goes through the SDK.
    """
    return (
        f"{endpoint}/subscriptions/{sub}"
        f"/resourceGroups/{rg}/sandboxGroups/{sg}/sandboxes/{sandbox_id}"
        f"/{suffix}?api-version={DATAPLANE_API_VERSION}"
    )


def _create_sandbox_with_gateway_connection(
    endpoint: str, sub: str, rg: str, sg: str,
    connection_resource_id: str, runtime_url: str, *,
    disk: str = "copilot", cpu: str = "2000m", memory: str = "4096Mi",
    labels: dict[str, str] | None = None,
) -> str:
    """PUT a sandbox via the regional dataplane with a ``gatewayConnections[]``
    entry referencing the Office 365 connection. Returns the assigned id.

    The per-sandbox entry mirrors the SG-level shape exactly —
    ``{resourceId, connectionRuntimeUrl, authentication.type=SystemAssignedManagedIdentity}`` —
    so the platform proxy has all the wiring it needs without a second
    lookup. With this set on BOTH the SG and the sandbox at create-time,
    the platform's connector-gateway-aware egress layer injects Bearer
    auth automatically on every outbound call to the runtime URL host.

    The published ``azure-containerapps-sandbox`` Python SDK does not yet
    expose ``gateway_connections`` on ``begin_create_sandbox``; we hit the
    dataplane directly so the caller can later wrap the id with
    ``SandboxGroupClient.get_sandbox_client(sid)``.

    Cascade's URL shape (verified live): no sandbox id in URL, no
    api-version, no apiVersion param — the server assigns the id and
    returns it in the response body.

    Post-PUT we read the sandbox back and assert the wiring landed.
    The dataplane MAY silently drop unknown fields (e.g., if it was
    provisioned on an API version that doesn't recognise
    ``gatewayConnections``), which would surface later as an opaque 401
    on the runtime URL smoke test — easier to debug here.
    """
    body: dict = {
        "sourcesRef": {"diskImage": {"name": disk, "isPublic": True}},
        "vmmType": "CloudHypervisor",
        "resources": {"cpu": cpu, "memory": memory, "disk": "20480Mi"},
        "gatewayConnections": [{
            "resourceId": connection_resource_id,
            "connectionRuntimeUrl": runtime_url,
            "authentication": {"type": "SystemAssignedManagedIdentity"},
        }],
    }
    if labels:
        body["labels"] = labels
    base = f"{endpoint}/subscriptions/{sub}/resourceGroups/{rg}/sandboxGroups/{sg}/sandboxes"
    _, out, _ = _az_rest("PUT", base, body=body, resource=DATAPLANE_RESOURCE)
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {}
    sid = ""
    if isinstance(data, dict):
        sid = data.get("id") or data.get("sandboxId") or data.get("name") or ""
        if isinstance(sid, str) and "/" in sid:
            sid = sid.rsplit("/", 1)[-1]
    if not sid:
        raise RuntimeError(f"dataplane sandbox PUT returned no id: {data!r}")

    # Critical: GET-back verify gatewayConnections actually persisted.
    # Without this, a silent drop on the dataplane side surfaces only as
    # an opaque 401 on the smoke test later.
    _, get_out, _ = _az_rest("GET", f"{base}/{sid}", resource=DATAPLANE_RESOURCE)
    try:
        sb = json.loads(get_out) if get_out else {}
    except json.JSONDecodeError:
        sb = {}
    gc = sb.get("gatewayConnections") or (sb.get("properties") or {}).get("gatewayConnections") or []
    want = connection_resource_id.lower()
    matched = None
    if isinstance(gc, list):
        for e in gc:
            if isinstance(e, dict) and isinstance(e.get("resourceId"), str) and e["resourceId"].lower() == want:
                matched = e
                break
    if matched is None:
        raise RuntimeError(
            "dataplane PUT accepted but gatewayConnections[] is NOT on the sandbox "
            f"after read-back. This is why the platform proxy would return 401 — "
            f"without a per-sandbox gatewayConnections entry the proxy has no MI "
            f"to fetch a token for. Sandbox GET response: {get_out[:2000]!r}"
        )
    auth_t = (matched.get("authentication") or {}).get("type") or ""
    runtime_back = matched.get("connectionRuntimeUrl") or ""
    print(f"    gatewayConnections[] present on sandbox (auth={auth_t}, runtime={runtime_back})")
    return sid


# ---------- egress policy --------------------------------------------------

def _build_egress_body() -> dict:
    """default Deny + host-Allow rules for GitHub Copilot CLI traffic.

    The Copilot CLI needs to reach `*.github.com` (device-code login),
    `*.githubusercontent.com`, `gh.io`, `*.github.io`, and
    `*.githubcopilot.com` (model + telemetry). It carries its own
    user-bearer once logged in, so we just allow these hosts.

    The Office 365 connection runtime URL host is NOT in the host-Allow
    list because the platform's ``gatewayConnections``-aware proxy
    mediates calls to it independently of the egress policy (verified
    live: ``defaultAction=Deny`` + no runtime-host allow still returns
    HTTP 200). The platform also injects Bearer auth on that path, so
    no Transform rule is needed either."""
    return {
        "defaultAction": "Deny",
        "hostRules": [
            {"pattern": "github.com",            "action": "Allow"},
            {"pattern": "*.github.com",          "action": "Allow"},
            {"pattern": "*.githubusercontent.com", "action": "Allow"},
            {"pattern": "gh.io",                 "action": "Allow"},
            {"pattern": "*.github.io",           "action": "Allow"},
            {"pattern": "githubcopilot.com",     "action": "Allow"},
            {"pattern": "*.githubcopilot.com",   "action": "Allow"},
        ],
    }


def _apply_egress(endpoint, sub, rg, sg, sandbox_id) -> None:
    url = _dp_url(endpoint, sub, rg, sg, sandbox_id, "egresspolicy")
    _az_rest("POST", url, body=_build_egress_body(),
             resource=DATAPLANE_RESOURCE)


# ---------- ports ----------------------------------------------------------

def _apply_ports(endpoint, sub, rg, sg, sandbox_id,
                 port: int, gw_principal: str, gw_tenant: str,
                 user_email: str) -> str:
    """POST /ports/add with ``entraId.objectIds=[gateway MI]`` (+ optional
    ``tenantIds``) AND ``activationMode=OnDemand`` so the proxy RESUMES
    the sandbox before forwarding the gateway's webhook POST.

    ``activationMode`` is a port-level field — POST /ports/add accepts
    it inline (the SDK's typed ``AddPortRequest`` declares it). We use
    POST /ports/add (not PUT /ports) so the platform assigns the proxy
    URL for us; PUT /ports is a "replace existing view" call that
    requires the url already exist. The SDK's typed ``PortAuthEntraId``
    doesn't carry ``objectIds`` today, so we hit the data plane
    directly.
    """
    entra_id: dict = {"enabled": True, "objectIds": [gw_principal]}
    if gw_tenant:
        entra_id["tenantIds"] = [gw_tenant]
    if user_email and "@" in user_email:
        entra_id["emails"] = [user_email]
    body = {
        "port": port,
        "auth": {"entraId": entra_id},
        "activationMode": "OnDemand",
    }
    url = _dp_url(endpoint, sub, rg, sg, sandbox_id, "ports/add")
    _, out, _ = _az_rest("POST", url, body=body, resource=DATAPLANE_RESOURCE)
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {}
    port_url = None
    if isinstance(data, dict):
        if isinstance(data.get("ports"), list):
            match = next((p for p in data["ports"]
                          if isinstance(p, dict) and p.get("port") == port), {})
            port_url = match.get("url")
        else:
            port_url = data.get("url")
    if not port_url:
        raise RuntimeError(f"ports/add returned no url: {data!r}")
    return port_url


def _remove_port(endpoint, sub, rg, sg, sandbox_id, port: int) -> None:
    url = _dp_url(endpoint, sub, rg, sg, sandbox_id, "ports/remove")
    _az_rest("POST", url, body={"port": port},
             resource=DATAPLANE_RESOURCE, check=False)


# ---------- main -----------------------------------------------------------

def main() -> int:
    _load_env()
    sub = os.environ["AZURE_SUBSCRIPTION_ID"]
    rg = os.environ["ACA_RESOURCE_GROUP"]
    sg = os.environ["ACA_SANDBOX_GROUP"]
    gw = os.environ["ACA_CONNECTOR_GATEWAY"]
    conn = os.environ["ACA_CONNECTOR_CONNECTION"]
    user_email = os.environ.get("ACA_USER_EMAIL", "").strip()
    triage_to = (
        os.environ.get("TRIAGE_RECIPIENT", "").strip() or user_email
    )
    if not triage_to or "@" not in triage_to:
        sys.exit(
            "error: TRIAGE_RECIPIENT not set and ACA_USER_EMAIL is empty.\n"
            "       Set one of them in .env."
        )

    # Re-resolve gateway MI, connection runtime URL, and SG MI from ARM on
    # every run, then preflight the wiring. This is the drift-prevention
    # core: the previous implementation read these from .env, but they go
    # stale silently every time the connection/gateway/SG is recreated,
    # producing 401 missing-authorization-header from the platform proxy
    # with no obvious clue why. See ../setup/connector_common.py for the
    # full check list.
    print("==> Re-resolving gateway / connection / sandbox group from ARM...")
    state = cc.resolve_all(sub, rg, gw, conn, sg)

    print("==> Preflight: validating wiring is in a state that injects auth...")
    errors = cc.preflight(sub, rg, gw, conn, state)
    if errors:
        sys.exit(
            "\n"
            "error: preflight detected drift between the connection, ACLs, and the sandbox\n"
            "       group's gatewayConnections[]. Fix the items marked ✗ above by running:\n"
            "\n"
            "         python ../setup/setup.py        # fast: repairs ACLs + re-PATCH SG\n"
            "         # OR (heavier, if setup itself is broken):\n"
            "         azd down --purge && azd up      # full rebuild from scratch\n"
        )
    print("    preflight passed.")

    # Derived values now come from ARM (cc.resolve_all populates these).
    gw_principal = state.gw_principal_id
    gw_tenant = state.gw_tenant_id
    runtime_url = state.runtime_url
    runtime_host = state.runtime_host
    # Use the sandbox group's actual location (from ARM) for the dataplane
    # endpoint — the .env value is only a default-at-creation-time and may
    # not match where the SG actually got placed.
    region = state.sg_region or os.environ.get("ACA_SANDBOXGROUP_REGION", "")
    if not region:
        sys.exit("error: sandbox group has no location and no ACA_SANDBOXGROUP_REGION in .env.")

    connection_resource_id = state.conn_resource_id

    # Resolve the GitHub token AFTER preflight — fail fast on drift rather
    # than prompting for a token, then bailing out anyway.
    print("==> Resolving GitHub token for Copilot CLI...")
    copilot_token = _resolve_copilot_token()

    arm_base = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/connectorGateways/{gw}"
    )
    config_name = "feedback-analyzer-demo"
    run_id = uuid.uuid4().hex[:8]

    endpoint = endpoint_for_region(region)
    credential = DefaultAzureCredential()
    client = SandboxGroupClient(
        endpoint, credential,
        subscription_id=sub, resource_group=rg, sandbox_group=sg,
    )
    sid: str = ""
    sandbox = None
    port_added = False
    trigger_created = False

    try:
        print(f"==> Creating sandbox in '{sg}' (labels.run={run_id}) with "
              f"gatewayConnections=[{conn}]...")
        # Sandbox MUST be created with gatewayConnections so the platform
        # injects Bearer auth on the runtime URL host. The published SDK
        # doesn't expose this field on begin_create_sandbox, so we PUT
        # the sandbox via the dataplane and then wrap the returned id
        # with get_sandbox_client(sid) for the typed exec/write_file
        # surface.
        sid = _create_sandbox_with_gateway_connection(
            endpoint, sub, rg, sg, connection_resource_id, runtime_url,
            disk="copilot", cpu="2000m", memory="4096Mi",
            labels={"sample": "connector-trigger-email", "run": run_id},
        )
        print(f"    sandbox: {sid}")
        sandbox = client.get_sandbox_client(sid)

        # The copilot disk image ships python3 + the github copilot CLI
        # pre-installed at /usr/local/bin/copilot; nothing to install.
        print("==> Verifying copilot CLI is present...")
        r = sandbox.exec("command -v copilot && copilot --version")
        if r.exit_code != 0:
            sys.exit(
                "error: copilot CLI not found on the sandbox.\n"
                "       The 'copilot' disk image should ship it; "
                "verify the image is up-to-date."
            )

        print(f"==> Uploading {APP_DIR.name}/server.py into /app...")
        sandbox.write_file(
            "/app/server.py",
            (APP_DIR / "server.py").read_text(encoding="utf-8"),
        )

        print(f"==> Starting listener on :{PORT} (setsid, logs at /tmp/listener.log)...")
        # The sandbox's executeShellCommand endpoint reaps any process group
        # spawned during the exec when the session ends (similar to kubectl
        # exec). Plain `nohup ... &` is NOT enough — nohup only catches
        # SIGHUP. We need `setsid` to start in a brand new session and
        # `</dev/null` to fully detach stdin. We write a small launcher.sh
        # to dodge nested-shell quoting headaches around env values.
        launcher = (
            "#!/bin/bash\n"
            "set -u\n"
            f"export PORT={shlex.quote(str(PORT))}\n"
            f"export O365_RUNTIME_URL={shlex.quote(runtime_url)}\n"
            f"export TRIAGE_RECIPIENT={shlex.quote(triage_to)}\n"
            # COPILOT_GITHUB_TOKEN is the documented headless-auth path
            # for the Copilot CLI (see `copilot help environment`). The
            # token only lives in this process's env + the sandbox's
            # process env; it is destroyed when the sandbox is deleted.
            f"export COPILOT_GITHUB_TOKEN={shlex.quote(copilot_token)}\n"
            "pkill -f 'python3 /app/server.py' 2>/dev/null || true\n"
            "sleep 1\n"
            "rm -f /tmp/listener.log /tmp/listener.pid\n"
            "setsid nohup python3 /app/server.py "
            "> /tmp/listener.log 2>&1 < /dev/null &\n"
            "disown || true\n"
            "echo $! > /tmp/listener.pid\n"
        )
        sandbox.write_file("/app/launch.sh", launcher)
        sandbox.exec("bash /app/launch.sh")
        _wait_in_sandbox(
            sandbox, f"http://localhost:{PORT}/healthz",
            log_path="/tmp/listener.log",
            pid_path="/tmp/listener.pid",
        )
        print("    listener is up")

        print(f"==> Locking down egress: Deny + GitHub host-allows "
              f"(runtime URL {runtime_host} mediated by platform)...")
        _apply_egress(endpoint, sub, rg, sg, sid)

        print(f"==> Gateway-connection auth-injection smoke test "
              f"(GET {runtime_host}/v2/Mail?top=1)...")
        _smoke_test_gateway_auth_injection(sandbox, runtime_url)
        print("    smoke test ok — platform injected auth, runtime URL returned 200")

        print("==> Verifying Copilot CLI auth (token round-trip)...")
        _verify_copilot_token(sandbox, copilot_token)

        print(f"==> add port {PORT} (entraId.objectIds=[gateway MI], tenantIds=[gateway tenant], activationMode=OnDemand)")
        port_url = _apply_ports(
            endpoint, sub, rg, sg, sid, PORT, gw_principal, gw_tenant, user_email,
        )
        port_added = True
        callback_url = port_url.rstrip("/") + "/webhook"

        print(f"==> PUT trigger config '{config_name}'...")
        trigger_body = {
            "properties": {
                "state": "Enabled",
                "connectionDetails": {
                    "connectorName": "office365",
                    "connectionName": conn,
                },
                "metadata": {
                    "sandboxGroupName": sg,
                    "sandboxId": sid,
                    # Poll every minute (default is 3 min) so the demo
                    # responds quickly. Override via the connector-gateway
                    # `recurrence` semantics if needed.
                    "recurrenceFrequency": "Minute",
                    "recurrenceInterval": 1,
                },
                "notificationDetails": {
                    "callbackUrl": callback_url,
                    "httpMethod": "POST",
                    # The connector gateway's managed identity authenticates
                    # the callback POST to the sandbox proxy. Without this
                    # block the proxy returns 401 (Bearer realm=proxy.<region>.
                    # azuredevcompute.io).
                    "authentication": {
                        "type": "ManagedServiceIdentity",
                        "audience": "https://auth.adcproxy.io/",
                    },
                },
                "operationName": "OnNewEmailV3",
                "parameters": (
                    [{"name": "folderPath", "value": "Inbox"}]
                    + ([{"name": "subjectFilter", "value": SUBJECT_FILTER}]
                       if SUBJECT_FILTER else [])
                ),
            }
        }
        _, t_out, _ = _az_rest(
            "PUT",
            f"{arm_base}/triggerConfigs/{config_name}?api-version={API_VERSION}",
            body=trigger_body,
            retry_on_5xx=3,
        )
        trigger_created = True
        try:
            state = json.loads(t_out).get("properties", {}).get("state", "?")
        except (json.JSONDecodeError, AttributeError):
            state = "?"

        print()
        print("=" * 72)
        print("Feedback-analyzer trigger is live")
        print("=" * 72)
        print(f"  trigger config:  {config_name} (state={state})")
        print(f"  listener URL:    {port_url}  (healthz only — webhook is gateway-only)")
        print(f"  callback URL:    {callback_url}")
        print(f"  reply goes to:   {triage_to}")
        print()
        print("To fire the trigger:")
        print(f"  1. Send yourself (or {user_email or 'the consent user'}) an email")
        if SUBJECT_FILTER:
            print(f"     whose subject contains the word '{SUBJECT_FILTER}' (case-insensitive).")
        else:
            print("     (any new email in the Inbox will fire the trigger).")
        print("  2. Wait 1-3 minutes - Office 365 polls every ~3 minutes.")
        print(f"  3. Watch {triage_to}'s inbox for a reply with subject")
        print("     'Auto-ack: received your message'.")
        print()
        print("Listener logs (from another terminal):")
        print(f"  aca sandbox exec -g {rg} --group {sg} \\")
        print(f"    --id {sid} --command 'tail -f /tmp/listener.log'")
        print()
        print("When done, press Enter here to tear everything down")
        print("(trigger -> port -> sandbox; gateway + connection are kept).")
        print("=" * 72)
        try:
            input("Press Enter to continue... ")
        except (EOFError, KeyboardInterrupt, OSError):
            print()

        return 0

    finally:
        # Cleanup order: trigger first (it holds an event subscription),
        # then port, then sandbox. Scenario baseline (gateway / connection /
        # access policy / SG gatewayConnections wiring) is NOT touched.
        if trigger_created:
            print("==> DELETE trigger config")
            _az_rest(
                "DELETE",
                f"{arm_base}/triggerConfigs/{config_name}"
                f"?api-version={API_VERSION}",
                check=False,
            )
        if sid and port_added:
            try:
                print(f"==> remove_port({PORT})")
                _remove_port(endpoint, sub, rg, sg, sid, PORT)
            except Exception as e:
                print(f"    warning: remove_port failed: {e}")
        if sid:
            try:
                print(f"==> delete sandbox {sid}")
                if sandbox is not None:
                    sandbox.delete()
                else:
                    # SDK wrap failed earlier but the dataplane create
                    # succeeded — go around the SDK and delete via raw
                    # dataplane to avoid hitting the same SDK gap.
                    delete_url = (
                        f"{endpoint}/subscriptions/{sub}"
                        f"/resourceGroups/{rg}/sandboxGroups/{sg}"
                        f"/sandboxes/{sid}"
                        f"?api-version={DATAPLANE_API_VERSION}"
                    )
                    _az_rest("DELETE", delete_url,
                             resource=DATAPLANE_RESOURCE, check=False)
            except Exception as e:
                print(f"    warning: delete sandbox failed: {e}")
        else:
            # Interrupted before the create call returned — sweep by label.
            print(f"==> Sweeping leaked sandboxes with run={run_id}...")
            try:
                for sbx in client.list_sandboxes(labels={"run": run_id}):
                    sid2 = getattr(sbx, "id", None) or getattr(sbx, "sandbox_id", None)
                    if not sid2:
                        continue
                    try:
                        client.delete_sandbox(sid2)
                        print(f"    deleted leaked sandbox {sid2}")
                    except Exception as e:
                        print(f"    warning: failed to delete {sid2}: {e}")
            except Exception as e:
                print(f"    warning: sweep failed: {e}")
        client.close()
        credential.close()


if __name__ == "__main__":
    sys.exit(main())
