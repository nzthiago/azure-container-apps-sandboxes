"""Shared Python helpers for the 10-connectors-triggers scenario.

Imported by both ``../setup/setup.py`` and ``../feedback-analyzer/run.py``
so that ARM re-resolution, ACL repair and preflight drift checks live in
ONE place — there's no second copy in ``run.py`` to drift from ``setup.py``.

Purpose: prevent stale-``.env`` drift. ``setup.py`` used to cache derived
values (runtime URL, gateway MI principalId, SG MI principalId) into the
``.env``, but those drift the moment the connection / gateway / sandbox
group is re-created. ``run.py`` would then call a host the platform
proxy doesn't recognise and the apihub returns 401
missing-authorization-header. Both scripts now re-resolve those values on
every invocation (3 ARM GETs, ~2s) and refuse to proceed if anything is
out of sync, printing the exact command to fix it.

The pair of this module is ``cli/.../setup/connector_common.sh`` — keep
the two implementations in lock-step. The bash version is the canonical
spec for what the checks do.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

API_VERSION = "2026-05-01-preview"
SANDBOXGROUP_API_VERSION = "2026-02-01-preview"

# Deprecated .env keys — the source-of-truth values are re-resolved from
# ARM on every run. Keeping these around as cached strings was the cause
# of the "stale runtime URL → 401" footgun this module exists to prevent.
DEPRECATED_ENV_KEYS = (
    "ACA_CONNECTOR_CONNECTION_RUNTIME_URL",
    "ACA_CONNECTOR_GATEWAY_PRINCIPAL_ID",
    "ACA_CONNECTOR_GATEWAY_TENANT_ID",
    "ACA_SANDBOX_GROUP_PRINCIPAL_ID",
)


def _az_rest_retry(method: str, url: str, *,
                   body: Optional[dict] = None,
                   resource: Optional[str] = None,
                   headers: Optional[dict] = None,
                   check: bool = True,
                   max_attempts: int = 5) -> tuple[int, str, str]:
    """Transient-retry wrapper for ``az rest``.

    Retries up to ``max_attempts`` times with exponential backoff (2/4/8/16s)
    on ARM control-plane blips (HTTP 429/502/503/504, gateway-timeout,
    "Service Unavailable", etc.). Returns ``(returncode, stdout, stderr)``.

    On hard failure with ``check=True`` calls ``sys.exit``.
    """
    cmd = ["az", "rest", "--method", method, "--url", url]
    if resource:
        cmd += ["--resource", resource]
    if headers:
        for k, v in headers.items():
            cmd += ["--headers", f"{k}={v}"]
    tmp = None
    if body is not None:
        fd, tmp = tempfile.mkstemp(prefix="cc-azr-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f)
        cmd += ["--body", f"@{tmp}"]
    try:
        attempt = 1
        while True:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               shell=sys.platform == "win32")
            if r.returncode == 0:
                return r.returncode, r.stdout, r.stderr
            err = (r.stderr or "")
            is_transient = (
                "Service Unavailable" in err
                or "Temporarily Unavailable" in err
                or "Gateway Timeout" in err
                or "Bad Gateway" in err
                or "(503)" in err or "(502)" in err
                or "(504)" in err or "(429)" in err
                or "TooManyRequests" in err
                or "internal server error" in err.lower()
                or "Request timed out" in err
            )
            if is_transient and attempt < max_attempts:
                delay = min(2 ** attempt, 16)
                print(f"    warning: az rest transient ARM error "
                      f"(attempt {attempt}/{max_attempts}); retry in {delay}s...",
                      file=sys.stderr)
                time.sleep(delay)
                attempt += 1
                continue
            if check:
                sys.exit(
                    f"error: az rest {method} {url} failed (exit={r.returncode}):\n"
                    f"{err.strip()[:800]}"
                )
            return r.returncode, r.stdout, r.stderr
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


@dataclass
class ResolvedState:
    """Snapshot of current ARM state for the gateway + connection + SG."""
    gw_principal_id: str = ""
    gw_tenant_id: str = ""
    gw_region: str = ""
    sg_principal_id: str = ""
    sg_tenant_id: str = ""
    sg_region: str = ""
    conn_status: str = ""
    conn_status_error: str = ""
    runtime_url: str = ""
    runtime_host: str = ""
    conn_resource_id: str = ""
    sg_gateway_connections: list = field(default_factory=list)


def resolve_all(sub: str, rg: str, gw: str, conn: str, sg: str) -> ResolvedState:
    """Re-resolve every derived value from ARM and return them as a
    :class:`ResolvedState`.

    Raises ``SystemExit`` if the gateway, connection, or sandbox group
    doesn't exist (with an actionable remediation message).
    """
    conn_resource_id = (
        f"/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}"
    )
    gw_url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/connectorGateways/{gw}?api-version={API_VERSION}"
    )
    conn_url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}"
        f"?api-version={API_VERSION}"
    )
    sg_url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.App/sandboxGroups/{sg}"
        f"?api-version={SANDBOXGROUP_API_VERSION}"
    )

    rc, gw_out, _ = _az_rest_retry("GET", gw_url, check=False)
    if rc != 0:
        sys.exit(
            f"error: gateway '{gw}' not found in RG '{rg}'.\n"
            f"       Re-run 'azd provision' (or 'python ../setup/setup.py') to recreate it."
        )
    rc, conn_out, _ = _az_rest_retry("GET", conn_url, check=False)
    if rc != 0:
        sys.exit(
            f"error: connection '{conn}' not found on gateway '{gw}'.\n"
            f"       Re-run 'azd provision' (or 'python ../setup/setup.py') to recreate it."
        )
    rc, sg_out, _ = _az_rest_retry("GET", sg_url, check=False)
    if rc != 0:
        sys.exit(
            f"error: sandbox group '{sg}' not found in RG '{rg}'.\n"
            f"       Re-run 'azd provision' (or 'python ../setup/setup.py') to recreate it."
        )

    try:
        gw_json   = json.loads(gw_out or "{}")
        conn_json = json.loads(conn_out or "{}")
        sg_json   = json.loads(sg_out or "{}")
    except json.JSONDecodeError as e:
        sys.exit(f"error: ARM returned non-JSON: {e}")

    def g(d, *path, default=""):
        cur = d
        for p in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(p)
            if cur is None:
                return default
        return cur if cur is not None else default

    statuses = g(conn_json, "properties", "statuses", default=[]) or []
    conn_status = ""
    conn_status_error = ""
    if statuses and isinstance(statuses[0], dict):
        conn_status = statuses[0].get("status") or ""
        err = statuses[0].get("error") or {}
        if isinstance(err, dict):
            conn_status_error = err.get("message") or err.get("code") or ""

    runtime_url = (g(conn_json, "properties", "connectionRuntimeUrl") or "").rstrip("/")
    runtime_host = ""
    if runtime_url:
        try:
            runtime_host = urllib.parse.urlparse(runtime_url).hostname or ""
        except Exception:
            runtime_host = ""

    return ResolvedState(
        gw_principal_id=g(gw_json, "identity", "principalId"),
        gw_tenant_id=g(gw_json, "identity", "tenantId"),
        gw_region=g(gw_json, "location"),
        sg_principal_id=g(sg_json, "identity", "principalId"),
        sg_tenant_id=g(sg_json, "identity", "tenantId"),
        sg_region=g(sg_json, "location"),
        conn_status=conn_status,
        conn_status_error=conn_status_error,
        runtime_url=runtime_url,
        runtime_host=runtime_host,
        conn_resource_id=conn_resource_id,
        sg_gateway_connections=g(sg_json, "properties", "gatewayConnections", default=[]) or [],
    )


def preflight(sub: str, rg: str, gw: str, conn: str, state: ResolvedState,
              *, stream=sys.stderr) -> list[str]:
    """Run drift checks. Print pass/fail lines + remediation to ``stream``.

    Returns a list of error descriptions (empty list = preflight passed).
    The caller decides whether to exit non-zero — but on a non-empty
    return list, ``run.py`` and ``setup.py`` both bail out.
    """
    errors: list[str] = []
    # Windows consoles default to cp1252 which can't encode ✓/✗. Try to
    # reconfigure the stream to UTF-8; if that's not supported, fall back
    # to ASCII markers so we never crash with UnicodeEncodeError.
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        PASS = "    ✓"
        FAIL = "    ✗"
    except (AttributeError, OSError, ValueError):
        PASS = "    [ok]"
        FAIL = "    [FAIL]"

    def out(msg: str) -> None:
        print(msg, file=stream, flush=True)

    if not state.gw_principal_id:
        out(f"{FAIL} gateway '{gw}' has no SystemAssigned managed identity.")
        out("      fix: re-run 'azd provision' (or 'python ../setup/setup.py')")
        errors.append("gateway missing MI")
    else:
        out(f"{PASS} gateway MI present (principalId={state.gw_principal_id})")

    if not state.sg_principal_id:
        out(f"{FAIL} sandbox group has no SystemAssigned managed identity.")
        out("      fix: aca sandboxgroup identity assign --system-assigned")
        errors.append("SG missing MI")
    else:
        out(f"{PASS} SG MI present (principalId={state.sg_principal_id})")

    if state.conn_status != "Connected":
        shown = state.conn_status or "?"
        out(f"{FAIL} connection '{conn}' status is '{shown}' (expected Connected).")
        if state.conn_status_error:
            out(f"      reason: {state.conn_status_error}")
        out("      fix: OAuth credential may have expired. "
            "Re-run 'azd provision' (or 'python ../setup/setup.py') to re-consent.")
        errors.append("connection not Connected")
    else:
        out(f"{PASS} connection status: Connected")

    if not state.runtime_url or not state.runtime_host:
        out(f"{FAIL} connection '{conn}' has no connectionRuntimeUrl "
            "(control plane hasn't minted it yet).")
        out("      fix: wait ~30s and retry; if persistent, re-run setup/setup.py.")
        errors.append("runtime URL missing")
    else:
        out(f"{PASS} runtime URL present ({state.runtime_host})")

    # ACLs
    acl_url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}"
        f"/accessPolicies?api-version={API_VERSION}"
    )
    rc, acl_out, _ = _az_rest_retry("GET", acl_url, check=False)
    acls = []
    if rc == 0:
        try:
            acls = (json.loads(acl_out or "{}") or {}).get("value", []) or []
        except json.JSONDecodeError:
            acls = []
    by_name = {}
    for a in acls:
        if isinstance(a, dict):
            obj = (((a.get("properties") or {}).get("principal") or {}).get("identity") or {}).get("objectId") or ""
            by_name[a.get("name")] = obj
    gw_obj = by_name.get("gateway-acl", "")
    sb_obj = by_name.get("sandbox-acl", "")
    if not (gw_obj and state.gw_principal_id and gw_obj.lower() == state.gw_principal_id.lower()):
        if not gw_obj:
            out(f"{FAIL} no 'gateway-acl' on connection '{conn}' "
                f"(found: {','.join(by_name.keys()) or 'none'}).")
        else:
            out(f"{FAIL} 'gateway-acl' points at stale principal '{gw_obj}' "
                f"(current gateway MI is '{state.gw_principal_id}').")
        out("      fix: re-run 'azd provision' (or 'python ../setup/setup.py') — it will repair stale ACLs.")
        errors.append("gateway-acl mismatch")
    else:
        out(f"{PASS} gateway-acl points at current gateway MI")
    if not (sb_obj and state.sg_principal_id and sb_obj.lower() == state.sg_principal_id.lower()):
        if not sb_obj:
            out(f"{FAIL} no 'sandbox-acl' on connection '{conn}' "
                f"(found: {','.join(by_name.keys()) or 'none'}).")
        else:
            out(f"{FAIL} 'sandbox-acl' points at stale principal '{sb_obj}' "
                f"(current SG MI is '{state.sg_principal_id}').")
        out("      fix: re-run 'azd provision' (or 'python ../setup/setup.py') — it will repair stale ACLs.")
        errors.append("sandbox-acl mismatch")
    else:
        out(f"{PASS} sandbox-acl points at current SG MI")

    # SG-level gatewayConnections[] entry: must contain our resourceId AND
    # have its connectionRuntimeUrl equal the connection's CURRENT runtime
    # URL AND have authentication.type == SystemAssignedManagedIdentity.
    found = None
    for e in state.sg_gateway_connections:
        if (isinstance(e, dict)
                and isinstance(e.get("resourceId"), str)
                and e["resourceId"].lower() == state.conn_resource_id.lower()):
            found = e
            break
    if found is None:
        out(f"{FAIL} sandbox group has no gatewayConnections[] entry for this connection.")
        out("      fix: re-run 'azd provision' (or 'python ../setup/setup.py') — it will PATCH the SG.")
        errors.append("SG gatewayConnections missing entry")
    else:
        actual_url = (found.get("connectionRuntimeUrl") or "").rstrip("/")
        auth_type = (((found.get("authentication") or {}).get("type")) or "")
        if actual_url != state.runtime_url or not state.runtime_url:
            out(f"{FAIL} sandbox group's gatewayConnections[] entry has STALE connectionRuntimeUrl:")
            out(f"         have:    {actual_url}")
            out(f"         expected:{state.runtime_url}")
            out("      (the connection was recreated since the SG was wired; THIS is the most common cause of 401 missing-authorization-header.)")
            out("      fix: re-run 'azd provision' (or 'python ../setup/setup.py') — it will re-PATCH the SG with the current URL.")
            errors.append("SG gatewayConnections stale runtime URL")
        elif auth_type != "SystemAssignedManagedIdentity":
            shown = auth_type or "missing"
            out(f"{FAIL} sandbox group's gatewayConnections[] entry has wrong "
                f"authentication.type='{shown}' (expected 'SystemAssignedManagedIdentity').")
            out("      fix: re-run 'azd provision' (or 'python ../setup/setup.py') — it will re-PATCH the SG.")
            errors.append("SG gatewayConnections wrong auth type")
        else:
            out(f"{PASS} SG gatewayConnections[] entry matches current runtime URL + auth type")

    return errors


def ensure_acl_current(
    sub: str, rg: str, gw: str, conn: str, region: str,
    name: str, principal_id: str, tenant_id: str,
) -> None:
    """Idempotent ACL writer that REPAIRS stale ACLs.

    Strategy: GET the ACL first. If its objectId matches, no-op. Otherwise
    PUT — and if the PUT is rejected with Exists/Conflict, DELETE + PUT.
    This prevents the silent-401 footgun where the gateway/SG MI was
    rotated (e.g. by recreating the resource via azd) but the connection
    still holds the old principalId in its accessPolicies.
    """
    base = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}"
        f"/accessPolicies/{name}"
    )
    get_url = f"{base}?api-version={API_VERSION}"
    rc, out, _ = _az_rest_retry("GET", get_url, check=False)
    current = ""
    if rc == 0:
        try:
            doc = json.loads(out or "{}")
            current = (((doc.get("properties") or {}).get("principal") or {})
                       .get("identity") or {}).get("objectId") or ""
        except json.JSONDecodeError:
            current = ""
    if current and current.lower() == principal_id.lower():
        print(f"    access policy '{name}' already current (objectId={current})")
        return
    if current:
        print(f"    access policy '{name}' is stale (have={current}, want={principal_id}) — replacing...")
        _az_rest_retry("DELETE", get_url, check=False)
    body = {
        "location": region,
        "properties": {
            "principal": {
                "type": "ActiveDirectory",
                "identity": {"objectId": principal_id, "tenantId": tenant_id},
            },
        },
    }
    rc, _, err = _az_rest_retry(
        "PUT", get_url, body=body,
        headers={"Content-Type": "application/json"}, check=False,
    )
    if rc == 0:
        print(f"    access policy '{name}' applied (objectId={principal_id})")
        return
    sys.exit(f"error: PUT access policy '{name}' failed:\n{err.strip()[:600]}")


def strip_deprecated_env_keys(env_file: Path) -> None:
    """Remove deprecated derived keys from a .env file in-place.

    Used at setup-time to evict cached derived values that would otherwise
    drift.
    """
    if not env_file.is_file():
        return
    lines = env_file.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    dropped: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            kept.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in DEPRECATED_ENV_KEYS:
            dropped.append(k)
            continue
        kept.append(line)
    for k in dropped:
        print(f"    .env: stripped deprecated key '{k}' (now re-resolved from ARM)")
    if dropped:
        env_file.write_text("\n".join(kept) + ("\n" if kept else ""),
                            encoding="utf-8")
