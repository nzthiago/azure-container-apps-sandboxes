"""Provision the connector-gateway baseline for the 10-connectors-triggers scenario (Python flow).

Creates (all idempotent):

  1. A connector gateway with SystemAssigned managed identity, in the
     resource group provisioned out-of-band (see README "Prerequisites").
  2. An Office 365 connection on that gateway.
  3. An access policy granting the gateway MI permission to use the
     connection (so it can subscribe to connector events on your behalf).
  4. The sandbox-group's SystemAssigned managed identity, plus a second
     access policy granting that MI permission to call the connection
     (so sandboxes can invoke connector actions like SendMailV2).
  5. A ``gatewayConnections[]`` entry on the sandbox group that wires the
     connection's runtime URL to the sandbox-group MI declaratively. Once
     this entry exists (and the per-sandbox ``gatewayConnections`` list
     references the same connection), the ADC platform injects
     ``Authorization: Bearer <SG-MI-token>`` on every outbound call to
     the runtime URL from any sandbox in the group — no per-sandbox
     egress Transform rule required.

Then appends the gateway/connection keys to ``.env`` (in the nearest
ancestor that already contains one, else the repo root) so the
sub-scenarios in this folder can find them.

If the Office 365 connection is not yet ``Connected``, this script
generates a one-time consent URL, opens it in your default browser,
and pauses until you press Enter. Click the link IMMEDIATELY — it
expires quickly.

Prerequisites:
  * Azure CLI installed and ``az login`` completed.
  * Python 3.10+.

If ``ACA_SANDBOX_GROUP`` is set, that group is used (and created in
the resource group if it doesn't exist). Otherwise the default name
``ai-apps-samples-group`` is created. The script will try to assign
the 'Container Apps SandboxGroup Data Owner' role to your principal
but will continue with a warning if that fails (you can re-grant it
later via ``aca sandboxgroup role create``).

Override defaults with environment variables:

  ACA_CONNECTOR_GATEWAY            default: ai-apps-samples-gw
  ACA_CONNECTOR_GATEWAY_REGION     default: ACA_SANDBOXGROUP_REGION
                                   (read from ``.env``; e.g. westus2)
  ACA_CONNECTOR_CONNECTION         default: o365-conn

Run:

  pip install -r requirements.txt
  python setup.py
  python setup.py --non-interactive   # don't open browser / wait for input;
                                       # exits with code 2 if consent is needed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

# Local shared helpers (ACL repair, ARM re-resolution, preflight,
# deprecated-keys sweep). These also back run.py so the rules live in
# exactly one place. See ./connector_common.py for the full rationale.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import connector_common as cc  # noqa: E402

API_VERSION = "2026-05-01-preview"
# Sandbox-group ARM resource uses a different (older) API version that
# expresses ``properties.gatewayConnections[]``.
SANDBOXGROUP_API_VERSION = "2026-02-01-preview"
CONNECTOR_NAME = "office365"

DEFAULTS = {
    "ACA_CONNECTOR_GATEWAY": "ai-apps-samples-gw",
    "ACA_CONNECTOR_CONNECTION": "o365-conn",
}


def _find_env_file() -> Path:
    """Walk up from this script looking for an existing ``.env``. Fall
    back to the repo root (the nearest ancestor containing ``.git``).
    Matches the convention used elsewhere in the repo (see
    ``cli/samples/00-get-started/00-sandbox-groups/run.sh``)."""
    here = Path(__file__).resolve().parent
    for d in (here, *here.parents):
        if (d / ".env").is_file():
            return d / ".env"
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d / ".env"
    sys.exit("error: could not determine where to write .env (no existing .env or .git found above this script).")


ENV_FILE = _find_env_file()


def _load_env() -> dict[str, str]:
    """Load .env into both os.environ and a dict for inspection."""
    if not ENV_FILE.exists():
        sys.exit(
            f"error: {ENV_FILE} does not exist. Run 'azd up' from this\n"
            "       scenario directory first to create one."
        )
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            values[k] = v
            # setdefault preserves process-env precedence: if the caller
            # already set the key, the value from .env never overrides.
            os.environ.setdefault(k, v)
    required = ("AZURE_SUBSCRIPTION_ID", "ACA_RESOURCE_GROUP")
    missing = [k for k in required if not values.get(k)]
    if missing:
        sys.exit(
            f"error: {ENV_FILE} is missing {missing}. Set these (e.g.\n"
            "       'azd env set ACA_RESOURCE_GROUP <name>') and re-run."
        )
    return values


def _az_rest(method: str, url: str, body: dict | None = None,
             resource: str | None = None, check: bool = True) -> tuple[int, str, str]:
    """Run `az rest` and return (returncode, stdout, stderr).

    Body, when supplied, is written to a temp JSON file and passed as
    ``--body @<file>`` to avoid shell-quoting issues on every platform.

    Retries up to ``_AZ_REST_MAX_ATTEMPTS`` times with exponential
    backoff on transient ARM failures (HTTP 429/502/503/504, "Service
    Unavailable", gateway-timeout, etc.) so a brief control-plane blip
    doesn't fail the whole setup.
    """
    cmd = ["az", "rest", "--method", method, "--url", url]
    if resource:
        cmd += ["--resource", resource]
    tmp_path = None
    if body is not None:
        fd, tmp_path = tempfile.mkstemp(prefix="aca-trig-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f)
        # Recent az CLI versions don't auto-set Content-Type when --body
        # references a file; pass it explicitly so PUT/POST/PATCH calls
        # don't 415 (UnsupportedMediaType).
        cmd += ["--headers", "Content-Type=application/json", "--body", f"@{tmp_path}"]
    try:
        result = None
        for attempt in range(1, _AZ_REST_MAX_ATTEMPTS + 1):
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                shell=sys.platform == "win32",
            )
            if result.returncode == 0:
                break
            combined = f"{result.stderr or ''}\n{result.stdout or ''}"
            if attempt < _AZ_REST_MAX_ATTEMPTS and _is_transient_arm_error(combined):
                delay = min(2 ** attempt, 16)
                print(
                    f"    warning: az rest {method} returned transient ARM error "
                    f"(attempt {attempt}/{_AZ_REST_MAX_ATTEMPTS}); retrying in {delay}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            break
        assert result is not None
        if check and result.returncode != 0:
            sys.exit(
                f"error: az rest {method} {url} failed (exit={result.returncode}):\n"
                f"{result.stderr.strip()[:800]}"
            )
        return result.returncode, result.stdout, result.stderr
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


_AZ_REST_MAX_ATTEMPTS = 5

_TRANSIENT_ARM_PATTERNS = (
    "service unavailable",
    "temporarily unavailable",
    "gateway timeout",
    "bad gateway",
    "(503)",
    "(502)",
    "(504)",
    "(429)",
    "toomanyrequests",
    "internal server error",
    "request timed out",
)


def _is_transient_arm_error(text: str) -> bool:
    """True if ``text`` (typically az-rest stderr+stdout) looks like a
    transient ARM control-plane error worth retrying on backoff."""
    if not text:
        return False
    haystack = text.lower()
    return any(p in haystack for p in _TRANSIENT_ARM_PATTERNS)


def _detect_subscription_id() -> str:
    value = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if value:
        return value
    try:
        out = subprocess.run(
            ["az", "account", "show", "-o", "json"],
            capture_output=True, text=True, check=True,
            shell=sys.platform == "win32",
        )
        return json.loads(out.stdout)["id"]
    except Exception as exc:
        sys.exit(
            "error: AZURE_SUBSCRIPTION_ID is unset and `az account show` "
            f"failed: {exc}"
        )


def _gateway_url(sub: str, rg: str, gw: str) -> str:
    return (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/connectorGateways/{gw}"
    )


def _ensure_gateway(sub: str, rg: str, gw: str, region: str) -> dict:
    """PUT the gateway with SystemAssigned identity; return parsed body."""
    url = f"{_gateway_url(sub, rg, gw)}?api-version={API_VERSION}"
    rc, out, _ = _az_rest("GET", url, check=False)
    if rc == 0 and out.strip():
        existing = json.loads(out)
        if existing.get("identity", {}).get("principalId"):
            print(f"    gateway '{gw}' already exists (skipping create)")
            return existing
    body = {"location": region, "identity": {"type": "SystemAssigned"}}
    _, out, _ = _az_rest("PUT", url, body=body)
    return json.loads(out)


def _ensure_connection(sub: str, rg: str, gw: str, conn: str, region: str) -> dict:
    """PUT the connection; return parsed body. The PUT response carries
    ``properties.connectionRuntimeUrl`` on first creation; on a subsequent
    GET (the idempotent branch) it should also be present once the
    connection is fully provisioned."""
    url = (
        f"{_gateway_url(sub, rg, gw)}/connections/{conn}"
        f"?api-version={API_VERSION}"
    )
    rc, out, _ = _az_rest("GET", url, check=False)
    if rc == 0 and out.strip():
        print(f"    connection '{conn}' already exists (skipping create)")
        return json.loads(out)
    body = {
        "location": region,
        "properties": {"connectorName": CONNECTOR_NAME},
    }
    _, out, _ = _az_rest("PUT", url, body=body)
    return json.loads(out)


def _connection_status(sub: str, rg: str, gw: str, conn: str) -> str:
    url = (
        f"{_gateway_url(sub, rg, gw)}/connections/{conn}"
        f"?api-version={API_VERSION}"
    )
    _, out, _ = _az_rest("GET", url)
    body = json.loads(out)
    statuses = body.get("properties", {}).get("statuses") or [{}]
    return statuses[0].get("status", "Unknown")


def _consent_url(sub: str, rg: str, gw: str, conn: str) -> str:
    """Generate the OAuth consent URL for an unauthorized connection."""
    conn_url = (
        f"{_gateway_url(sub, rg, gw)}/connections/{conn}"
        f"?api-version={API_VERSION}"
    )
    _, out, _ = _az_rest("GET", conn_url)
    conn_body = json.loads(out)
    created_by = conn_body.get("properties", {}).get("createdBy") or {}
    object_id = created_by.get("name") or created_by.get("objectId")
    tenant_id = created_by.get("tenantId")
    if not (object_id and tenant_id):
        sys.exit(
            "error: connection has no createdBy.{name,tenantId}; cannot "
            "build a consent link. Try deleting the connection and re-running."
        )
    body = {
        "parameters": [{
            "objectId": object_id,
            "tenantId": tenant_id,
            "redirectUrl": "https://microsoft.com",
            "parameterName": "token",
        }],
    }
    list_url = (
        f"{_gateway_url(sub, rg, gw)}/connections/{conn}/listConsentLinks"
        f"?api-version={API_VERSION}"
    )
    _, out, _ = _az_rest("POST", list_url, body=body)
    payload = json.loads(out)
    links = payload.get("value") or []
    if not links:
        sys.exit(f"error: listConsentLinks returned no links: {payload}")
    return links[0]["link"]


def _ensure_access_policy(
    sub: str, rg: str, gw: str, conn: str, region: str,
    principal_id: str, tenant_id: str, *, name: str,
) -> None:
    """Grant a principal (gateway MI or sandbox-group MI) use of the connection.

    The same body shape covers both grants — only the policy ``name`` and
    ``principal_id`` change.
    """
    url = (
        f"{_gateway_url(sub, rg, gw)}/connections/{conn}/accessPolicies/{name}"
        f"?api-version={API_VERSION}"
    )
    body = {
        "location": region,
        "properties": {
            "principal": {
                "type": "ActiveDirectory",
                "identity": {"objectId": principal_id, "tenantId": tenant_id},
            },
        },
    }
    rc, _, err = _az_rest("PUT", url, body=body, check=False)
    if rc == 0:
        print(f"    access policy '{name}' applied")
        return
    if "Exists" in err or "Conflict" in err:
        print(f"    access policy '{name}' already exists (skipping)")
        return
    sys.exit(f"error: access-policy '{name}' PUT failed:\n{err.strip()[:600]}")


def _connection_runtime_url(sub: str, rg: str, gw: str, conn: str,
                            poll_timeout: int = 60) -> str:
    """Return the connection's ``connectionRuntimeUrl``.

    Polls for a few seconds because the property may not be set on the
    very first GET after consent completes (control plane has to mint
    the runtime URL once the OAuth secret is in place).
    """
    url = (
        f"{_gateway_url(sub, rg, gw)}/connections/{conn}"
        f"?api-version={API_VERSION}"
    )
    deadline = time.monotonic() + poll_timeout
    runtime = ""
    while time.monotonic() < deadline:
        _, out, _ = _az_rest("GET", url)
        body = json.loads(out)
        runtime = body.get("properties", {}).get("connectionRuntimeUrl") or ""
        if runtime:
            return runtime.rstrip("/")
        time.sleep(3)
    sys.exit(
        f"error: connection '{conn}' has no properties.connectionRuntimeUrl after "
        f"{poll_timeout}s.\n"
        "       This usually means the connection isn't fully provisioned;\n"
        "       wait ~30s and re-run setup."
    )


# ---------- sandbox-group gatewayConnections wiring ----------------------

def _sandboxgroup_url(sub: str, rg: str, sg: str) -> str:
    return (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.App/sandboxGroups/{sg}"
        f"?api-version={SANDBOXGROUP_API_VERSION}"
    )


def _ensure_sandboxgroup_gateway_connection(
    sub: str, rg: str, sg: str,
    connection_resource_id: str, runtime_url: str,
) -> None:
    """GET-merge-PATCH the sandbox group's ``properties.gatewayConnections[]``
    so it contains our entry without clobbering anything already there
    (e.g. MCP server entries set up by other samples).

    This is the declarative wiring Cascade uses for MCP servers; we
    extend it to plain Office 365 (verified live in BrazilSouth).
    With the entry in place AND each sandbox declaring the same
    ``resourceId`` in its own create body, the platform injects
    ``Authorization: Bearer <SG-MI-token>`` on every outbound call to
    ``runtime_url`` from any sandbox in the group — no per-sandbox
    egress Transform rule required.

    Idempotent: if an entry with the same ``resourceId`` already exists,
    we update its ``connectionRuntimeUrl`` (in case the connection was
    re-provisioned) and re-PATCH only if the body actually changed.
    """
    url = _sandboxgroup_url(sub, rg, sg)
    _, out, _ = _az_rest("GET", url)
    body = json.loads(out)
    props = body.get("properties") or {}
    existing = list(props.get("gatewayConnections") or [])
    rid_lower = connection_resource_id.lower()
    new_fields = {
        "resourceId": connection_resource_id,
        "connectionRuntimeUrl": runtime_url,
        "authentication": {"type": "SystemAssignedManagedIdentity"},
    }
    merged: list[dict] = []
    replaced = False
    for e in existing:
        if (isinstance(e, dict)
                and isinstance(e.get("resourceId"), str)
                and e["resourceId"].lower() == rid_lower):
            # Merge into the existing dict so any future/unknown fields
            # the service may have added (e.g. policy refs, status) are
            # preserved across rewrites. ARM resource IDs are
            # case-insensitive, so compare lowercased to avoid
            # accidentally appending a duplicate entry.
            merged.append({**e, **new_fields})
            replaced = True
        else:
            merged.append(e)
    if not replaced:
        merged.append(dict(new_fields))
    if merged == existing:
        print(f"    sandbox-group gatewayConnections already includes '{connection_resource_id.split('/')[-1]}' (skipping)")
        return
    patch_body = {"properties": {"gatewayConnections": merged}}
    _az_rest("PATCH", url, body=patch_body)
    action = "updated" if replaced else "added"
    print(f"    sandbox-group gatewayConnections[] {action} entry for "
          f"'{connection_resource_id.split('/')[-1]}'")


def _aca(*args: str, capture: bool = False, check: bool = True) -> str:
    """Run ``aca`` with the given args; return stdout (if ``capture``) or empty.

    With ``check=False``, returns stdout (or empty string) on non-zero
    exit without sys.exit-ing — useful for probes where "no identity yet"
    is a normal state.
    """
    cmd = ["aca", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            shell=sys.platform == "win32",
        )
    except FileNotFoundError:
        sys.exit(
            "error: the 'aca' CLI is required for this setup but was not\n"
            "       found on PATH. Install it and retry:\n"
            "         https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md"
        )
    if check and result.returncode != 0:
        sys.exit(
            f"error: {' '.join(cmd)} failed (exit={result.returncode}):\n"
            f"{(result.stderr or result.stdout).strip()[:600]}"
        )
    if not capture:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _ensure_sandbox_group_exists(sg: str, want_region: str) -> str:
    """Ensure the named sandbox group exists, creating it in ``want_region``
    if not. Returns the group's actual location (which may differ from
    ``want_region`` if the group already existed elsewhere)."""
    existing = _aca(
        "sandboxgroup", "get", "--name", sg, "-o", "json",
        capture=True, check=False,
    ).strip()
    if existing and existing != "null":
        try:
            body = json.loads(existing)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict):
            loc = body.get("location") or want_region
            print(f"    sandbox group '{sg}' already exists (location={loc})")
            return loc
    print(f"==> Creating sandbox group '{sg}' in {want_region}...")
    result = subprocess.run(
        ["aca", "sandboxgroup", "create", "--name", sg, "--location", want_region],
        capture_output=True, text=True, shell=sys.platform == "win32",
    )
    if result.returncode != 0:
        sys.exit(
            f"error: 'aca sandboxgroup create --name {sg} --location {want_region}' failed:\n"
            f"{(result.stderr or result.stdout).strip()[:800]}"
        )
    return want_region


def _ensure_sandbox_group_role(sg: str) -> None:
    """Try to grant 'Container Apps SandboxGroup Data Owner' on ``sg`` to
    the current principal. Warn (don't fail) on errors — assignment may
    already exist, or the caller may already have access via another path
    (e.g. inheritance, group membership)."""
    principal_id = ""
    for cmd in (
        ["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"],
        ["az", "account", "show", "--query", "user.assignedIdentityInfo", "-o", "tsv"],
    ):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                shell=sys.platform == "win32",
            )
            if result.returncode == 0 and result.stdout.strip():
                principal_id = result.stdout.strip()
                break
        except Exception:
            continue

    if not principal_id:
        print(
            "    warning: could not detect current principal; skipping role assignment.\n"
            "    If later calls hit 403, run manually with your principal id:\n"
            f"      aca sandboxgroup role create --name '{sg}' \\\n"
            "        --role 'Container Apps SandboxGroup Data Owner' \\\n"
            "        --principal-id <your-principal-id>"
        )
        return

    print(f"    granting 'Container Apps SandboxGroup Data Owner' to {principal_id} on '{sg}'...")
    result = subprocess.run(
        ["aca", "sandboxgroup", "role", "create",
         "--name", sg,
         "--role", "Container Apps SandboxGroup Data Owner",
         "--principal-id", principal_id],
        capture_output=True, text=True, shell=sys.platform == "win32",
    )
    if result.returncode != 0:
        print(
            "    note: role assignment did not complete (may already exist, or insufficient perms).\n"
            "    If later calls hit 403, run manually:\n"
            f"      aca sandboxgroup role create --name '{sg}' \\\n"
            "        --role 'Container Apps SandboxGroup Data Owner' \\\n"
            f"        --principal-id {principal_id}"
        )


def _ensure_sandbox_group_identity(rg: str, sg: str) -> str:
    """Return the sandbox-group SystemAssigned MI principalId, enabling
    identity if it isn't already on.

    Sandbox groups are created with no identity by default. The send-side
    access policy needs the sandbox-group MI as a principal, so this
    script ensures it exists and surfaces the principalId for callers
    to write into ``.env``.

    The ``aca sandboxgroup`` commands address the group by ``--name`` only;
    the resource group + subscription are read from ``aca`` CLI config
    (or the ``ACA_RESOURCE_GROUP`` / ``ACA_SUBSCRIPTION`` env vars already
    in ``.env``). ``rg`` is accepted only so error messages can
    refer to it.
    """
    def _read_principal() -> str:
        # `aca sandboxgroup identity show` returns identity info
        # ({principalId, tenantId, type}) for the group identified by
        # --name. The resource group + subscription are read from `aca`
        # CLI config (or ACA_RESOURCE_GROUP / ACA_SUBSCRIPTION env vars
        # already in .env). Returns exit code != 0 if the group
        # has no identity assigned yet.
        out = _aca(
            "sandboxgroup", "identity", "show", "--name", sg, "-o", "json",
            capture=True, check=False,
        )
        if not out.strip():
            return ""
        try:
            body = json.loads(out)
        except json.JSONDecodeError:
            sys.exit(
                f"error: could not parse 'aca sandboxgroup identity show' output:\n{out[:400]}"
            )
        # `aca sandboxgroup identity show` prints the literal JSON `null`
        # (which json.loads turns into Python None) when the group has no
        # identity assigned yet. Treat that as "no principal".
        if not isinstance(body, dict):
            return ""
        return body.get("principalId") or ""

    principal = _read_principal()
    if principal:
        print(f"    sandbox-group MI already enabled (principalId={principal})")
        return principal

    print(f"    enabling SystemAssigned identity on '{sg}' (rg={rg})...")
    _aca("sandboxgroup", "identity", "assign", "--name", sg, "--system-assigned")
    # principalId may take a moment to appear after the assign call returns.
    for _ in range(12):
        principal = _read_principal()
        if principal:
            print(f"    sandbox-group MI ready (principalId={principal})")
            return principal
        time.sleep(2)
    sys.exit(
        "error: sandbox-group identity assign returned but principalId never\n"
        "       appeared. Re-run setup."
    )


def _write_env_file(values: dict[str, str]) -> None:
    """Merge values into .env, preserving keys we don't own."""
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    existing.update(values)
    lines = [
        "# Updated by python/samples/10-connectors-triggers/setup/setup.py",
        "# Re-run scenario setup to update.",
        "",
    ]
    for key in sorted(existing):
        lines.append(f"{key}={existing[key]}")
    ENV_FILE.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    print(f"    wrote {ENV_FILE}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision the connector-gateway baseline for the 10-connectors-triggers scenario."
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Don't open browser or wait for input. Exits with code 2 if "
             "OAuth consent is still needed; re-run after completing consent.",
    )
    args = parser.parse_args()

    env = _load_env()
    subscription_id = _detect_subscription_id()
    resource_group = env.get("ACA_RESOURCE_GROUP") or os.environ["ACA_RESOURCE_GROUP"]
    gateway = os.environ.get("ACA_CONNECTOR_GATEWAY", DEFAULTS["ACA_CONNECTOR_GATEWAY"])
    # Default the gateway region to the sandbox-group region so they're
    # colocated. ACA_SANDBOXGROUP_REGION (canonical) and ACA_REGION
    # (legacy alias) are both read from .env if present.
    region = (
        os.environ.get("ACA_CONNECTOR_GATEWAY_REGION")
        or env.get("ACA_CONNECTOR_GATEWAY_REGION")
        or env.get("ACA_SANDBOXGROUP_REGION")
        or env.get("ACA_REGION")
        or os.environ.get("ACA_SANDBOXGROUP_REGION")
        or os.environ.get("ACA_REGION")
    )
    if not region:
        sys.exit(
            "error: could not determine gateway region. Either set "
            "ACA_CONNECTOR_GATEWAY_REGION explicitly, or set "
            "ACA_SANDBOXGROUP_REGION in .env."
        )
    conn = os.environ.get("ACA_CONNECTOR_CONNECTION", DEFAULTS["ACA_CONNECTOR_CONNECTION"])

    # Resolve / prompt for ACA_USER_EMAIL up-front so feedback-analyzer/run.py
    # (which uses it as the default TRIAGE_RECIPIENT) doesn't bail later.
    # Must match the mailbox the user will sign in with at OAuth consent.
    user_email = (
        os.environ.get("ACA_USER_EMAIL")
        or env.get("ACA_USER_EMAIL")
        or ""
    ).strip()
    if not user_email:
        if args.non_interactive:
            print(
                "warning: ACA_USER_EMAIL not set and running --non-interactive;\n"
                "         feedback-analyzer/run.py will require TRIAGE_RECIPIENT\n"
                "         or ACA_USER_EMAIL to be set before it can run.",
                file=sys.stderr,
            )
        else:
            print()
            print("==> Office 365 mailbox required")
            print("    The next step pops an OAuth-consent link for an O365 mailbox.")
            print("    The feedback-analyzer demo then uses that mailbox to send a test")
            print("    'Feedback' email AND as the default destination for the reply.")
            print("    Enter the email (UPN) of that mailbox now — it must match the")
            print("    account you sign in with at the consent step.")
            while True:
                try:
                    candidate = input("    Office 365 email: ").strip()
                except EOFError:
                    candidate = ""
                if "@" in candidate and "." in candidate.split("@", 1)[1]:
                    user_email = candidate
                    break
                print("    invalid; expected e.g. you@contoso.com")
    if user_email:
        os.environ["ACA_USER_EMAIL"] = user_email

    # Resolve / create the sandbox group BEFORE we print the banner so
    # the colocated-region adjustment (if any) is reflected in the
    # printed config.
    sandbox_group_name = (
        os.environ.get("ACA_SANDBOX_GROUP")
        or env.get("ACA_SANDBOX_GROUP")
        or "ai-apps-samples-group"
    )
    if not (env.get("ACA_SANDBOX_GROUP") or os.environ.get("ACA_SANDBOX_GROUP")):
        # Print BEFORE we set ACA_SANDBOX_GROUP=default so the message is
        # only emitted on the first run.
        print(f"==> ACA_SANDBOX_GROUP not set; using default '{sandbox_group_name}'.")
    os.environ["ACA_SANDBOX_GROUP"] = sandbox_group_name

    print(f"==> Ensuring sandbox group '{sandbox_group_name}'...")
    sandbox_group_location = _ensure_sandbox_group_exists(sandbox_group_name, region)
    _ensure_sandbox_group_role(sandbox_group_name)
    if sandbox_group_location and sandbox_group_location.lower() != region.lower():
        print(
            f"    note: sandbox group is in '{sandbox_group_location}'; "
            f"colocating gateway there (was '{region}')."
        )
        region = sandbox_group_location

    print("==> Connector-gateway scenario - Python setup")
    print(f"    subscription:    {subscription_id}")
    print(f"    resource group:  {resource_group}")
    print(f"    sandbox group:   {sandbox_group_name} ({sandbox_group_location})")
    print(f"    gateway:         {gateway}")
    print(f"    gateway region:  {region}")
    print(f"    connection:      {conn} ({CONNECTOR_NAME})")

    print(f"==> Ensuring connector gateway '{gateway}' in {region}...")
    gw_body = _ensure_gateway(subscription_id, resource_group, gateway, region)
    identity = gw_body.get("identity") or {}
    principal_id = identity.get("principalId")
    tenant_id = identity.get("tenantId")
    if not (principal_id and tenant_id):
        sys.exit(
            "error: gateway has no system-assigned identity. Delete the\n"
            "       gateway and re-run setup."
        )
    print(f"    gateway MI principalId={principal_id}")
    print(f"    gateway MI tenantId   ={tenant_id}")

    print(f"==> Ensuring '{CONNECTOR_NAME}' connection '{conn}'...")
    _ensure_connection(subscription_id, resource_group, gateway, conn, region)

    status = _connection_status(subscription_id, resource_group, gateway, conn)
    print(f"    connection status: {status}")
    if status != "Connected":
        link = _consent_url(subscription_id, resource_group, gateway, conn)
        print()
        print("=" * 72)
        print("Office 365 connection needs OAuth consent.")
        print()
        print("  1. The link below is short-lived - click it IMMEDIATELY.")
        print("  2. Sign in with the account whose inbox you want to wire.")
        print("  3. After you see 'You may close this window', return here.")
        print()
        print(f"  Consent URL:\n  {link}")
        print("=" * 72)
        # Best-effort browser launch. We do this in BOTH interactive and
        # --non-interactive modes - the URL is short-lived (~5 min) so we
        # want to give the user a fighting chance to see it even when
        # automation is driving the script.
        opened = False
        try:
            opened = webbrowser.open(link)
        except Exception:
            opened = False
        if not opened and sys.platform == "win32":
            # webbrowser.open can return False on headless Windows; try
            # cmd.exe start as a backup.
            try:
                subprocess.run(
                    ["cmd.exe", "/c", "start", "", link],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                opened = True
            except Exception:
                opened = False
        if opened:
            print("    opened consent URL in your default browser.")
        else:
            print("    could not auto-open browser; copy the URL above manually.")
        if args.non_interactive:
            print()
            print("--non-interactive set; not waiting for consent to complete.")
            print("Complete consent in the browser, then re-run this script.")
            sys.exit(2)
        try:
            input("\nPress Enter once consent is complete... ")
        except (EOFError, KeyboardInterrupt):
            print()
        for _ in range(6):
            status = _connection_status(subscription_id, resource_group, gateway, conn)
            if status == "Connected":
                break
            time.sleep(5)
        if status != "Connected":
            sys.exit(
                f"error: connection still shows status '{status}'.\n"
                "       Re-run setup; the consent link expires quickly."
            )
        print(f"    connection status: {status}")

    print("==> Granting gateway MI access policy on its own connection...")
    cc.ensure_acl_current(
        subscription_id, resource_group, gateway, conn, region,
        name="gateway-acl",
        principal_id=principal_id, tenant_id=tenant_id,
    )

    print(f"==> Ensuring sandbox-group '{sandbox_group_name}' has SystemAssigned identity...")
    sg_principal_id = _ensure_sandbox_group_identity(resource_group, sandbox_group_name)

    print("==> Granting sandbox-group MI access policy on the same connection (send-side)...")
    cc.ensure_acl_current(
        subscription_id, resource_group, gateway, conn, region,
        name="sandbox-acl",
        principal_id=sg_principal_id, tenant_id=tenant_id,
    )

    print("==> Resolving connection runtime URL...")
    # Always do a fresh GET (with polling) here rather than reusing a
    # pre-consent connection PUT response. The runtime URL may be empty
    # at PUT time (first create) or get rotated when the connection
    # transitions to ``Connected``. A fresh poll guarantees we wire the
    # SG and write ``.env`` with the URL the platform proxy will
    # actually accept.
    runtime_url = _connection_runtime_url(
        subscription_id, resource_group, gateway, conn,
    )
    print(f"    connectionRuntimeUrl: {runtime_url}")

    print(f"==> Wiring connection on sandbox group '{sandbox_group_name}' "
          f"(gatewayConnections[] + SystemAssignedManagedIdentity auth)...")
    connection_resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Web/connectorGateways/{gateway}/connections/{conn}"
    )
    _ensure_sandboxgroup_gateway_connection(
        subscription_id, resource_group, sandbox_group_name,
        connection_resource_id, runtime_url,
    )

    print(f"==> Writing {ENV_FILE}...")
    # Strip deprecated derived keys from any pre-existing .env so users
    # updating from older versions don't carry stale values forward.
    # Derived values (runtime URL, gateway/SG MI principalIds) are now
    # re-resolved from ARM by feedback-analyzer/run.py on every run.
    cc.strip_deprecated_env_keys(ENV_FILE)
    env_to_write = {
        "ACA_SANDBOX_GROUP": sandbox_group_name,
        "ACA_SANDBOXGROUP_REGION": sandbox_group_location,
        "ACA_CONNECTOR_GATEWAY": gateway,
        "ACA_CONNECTOR_GATEWAY_REGION": region,
        "ACA_CONNECTOR_CONNECTION": conn,
    }
    if user_email:
        env_to_write["ACA_USER_EMAIL"] = user_email
    _write_env_file(env_to_write)

    # ----- Self-test preflight -------------------------------------------
    # Don't declare success on a broken state. Re-resolve everything we
    # just wrote and assert wiring is correct end-to-end. If a check
    # fails here, run.py would silently 401 — much better to surface it
    # now while OAuth consent / ARM context is fresh.
    print("==> Self-test preflight: re-resolving from ARM and validating wiring...")
    state = cc.resolve_all(
        subscription_id, resource_group, gateway, conn, sandbox_group_name,
    )
    errors = cc.preflight(
        subscription_id, resource_group, gateway, conn, state,
    )
    if errors:
        sys.exit(
            "error: self-test preflight failed (see ✗ items above).\n"
            "       Setup left the gateway in an inconsistent state. Try re-running setup;\n"
            "       if it persists, run 'azd down --purge' then 'azd up' for a clean rebuild."
        )
    print("    self-test passed.")

    print("==> Done.")
    print()
    print("Next:  cd ../feedback-analyzer && pip install -r requirements.txt && python run.py")


if __name__ == "__main__":
    main()
