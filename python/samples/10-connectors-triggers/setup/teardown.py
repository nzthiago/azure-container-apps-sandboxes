"""Tear down the connector-gateway baseline for the 10-connectors-triggers scenario.

Deletes the connector gateway (along with all its connections, trigger
configs, and access policies), removes the scenario's entry from the
sandbox group's ``properties.gatewayConnections[]`` (preserving any
other entries like MCP servers), then clears the trigger-related keys
from ``.env``.

This script does NOT touch the resource group or sandbox group itself.

  python teardown.py
  python teardown.py --yes      # skip confirmation
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

API_VERSION = "2026-05-01-preview"
SANDBOXGROUP_API_VERSION = "2026-02-01-preview"


def _find_env_file() -> Path | None:
    here = Path(__file__).resolve().parent
    for d in (here, *here.parents):
        if (d / ".env").is_file():
            return d / ".env"
    return None


ENV_FILE = _find_env_file()

TRIGGER_OWNED_KEYS = (
    "ACA_CONNECTOR_GATEWAY",
    "ACA_CONNECTOR_GATEWAY_REGION",
    "ACA_CONNECTOR_CONNECTION",
    "ACA_CONNECTOR_GATEWAY_PRINCIPAL_ID",
    "ACA_CONNECTOR_GATEWAY_TENANT_ID",
    "ACA_CONNECTOR_CONNECTION_RUNTIME_URL",
    "ACA_SANDBOX_GROUP_PRINCIPAL_ID",
)


def _load_env() -> None:
    if not ENV_FILE or not ENV_FILE.exists():
        sys.exit("error: .env not found - nothing to tear down.")
    for line in ENV_FILE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _az_rest(method: str, url: str, body: dict | None = None) -> tuple[int, str, str]:
    cmd = ["az", "rest", "--method", method, "--url", url]
    tmp_path = None
    if body is not None:
        fd, tmp_path = tempfile.mkstemp(prefix="aca-trig-td-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f)
        cmd += ["--body", f"@{tmp_path}"]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            shell=sys.platform == "win32",
        )
        return r.returncode, r.stdout, r.stderr
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _az_rest_delete(url: str) -> None:
    rc, _, err = _az_rest("DELETE", url)
    if rc != 0:
        s = (err or "").strip()[:600]
        if "ResourceNotFound" in s or "NotFound" in s or "404" in s:
            print("    not found (already deleted)")
            return
        sys.exit(f"error: az rest DELETE failed:\n{s}")


def _remove_sandboxgroup_gateway_connection(
    sub: str, rg: str, sg: str, connection_resource_id: str,
) -> None:
    """GET-filter-PATCH the sandbox group's gatewayConnections[] to remove
    only the entry referencing our connection. Preserves any other
    entries (e.g. MCP servers set up by other samples).

    Best-effort: if the sandbox group is gone or has no entry, log and
    continue."""
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.App/sandboxGroups/{sg}"
        f"?api-version={SANDBOXGROUP_API_VERSION}"
    )
    rc, out, err = _az_rest("GET", url)
    if rc != 0:
        s = (err or "").strip()[:300]
        if "ResourceNotFound" in s or "NotFound" in s or "404" in s:
            print(f"    sandbox group '{sg}' not found (skipping)")
            return
        print(f"    warning: GET sandbox group failed; skipping cleanup: {s}")
        return
    try:
        body = json.loads(out) if out else {}
    except json.JSONDecodeError:
        print("    warning: could not parse sandbox group body; skipping cleanup")
        return
    existing = list((body.get("properties") or {}).get("gatewayConnections") or [])
    # ARM resource IDs are case-insensitive; compare lowercased so we
    # still remove an entry written by a setup run that used a different
    # case for sub/rg/gateway/connection segments.
    rid_lower = connection_resource_id.lower()
    remaining = [e for e in existing
                 if not (isinstance(e, dict)
                         and isinstance(e.get("resourceId"), str)
                         and e["resourceId"].lower() == rid_lower)]
    if len(remaining) == len(existing):
        print(f"    sandbox group has no gatewayConnections entry for this connection (skipping)")
        return
    patch_body = {"properties": {"gatewayConnections": remaining}}
    rc, _, err = _az_rest("PATCH", url, body=patch_body)
    if rc != 0:
        print(f"    warning: PATCH sandbox group failed; entry may be stale: {err.strip()[:300]}")
        return
    print(f"    removed gatewayConnections entry for '{connection_resource_id.split('/')[-1]}' "
          f"(kept {len(remaining)} other entries)")


def _rewrite_env_dropping(keys: tuple[str, ...]) -> None:
    if not ENV_FILE or not ENV_FILE.exists():
        return
    kept: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k not in keys:
                kept[k] = v
    lines = [
        "# Updated by python/samples/10-connectors-triggers/setup/teardown.py",
        "# Re-run scenario setup to update.",
        "",
    ]
    for key in sorted(kept):
        lines.append(f"{key}={kept[key]}")
    ENV_FILE.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    print(f"    wrote {ENV_FILE} (dropped {len(keys)} trigger keys)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="skip confirmation")
    args = parser.parse_args()

    _load_env()
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
    resource_group = os.environ.get("ACA_RESOURCE_GROUP")
    gateway = os.environ.get("ACA_CONNECTOR_GATEWAY")
    connection = os.environ.get("ACA_CONNECTOR_CONNECTION")
    sandbox_group = os.environ.get("ACA_SANDBOX_GROUP")
    if not (subscription_id and resource_group and gateway):
        sys.exit(
            "error: .env missing trigger keys - was connector setup run?"
        )

    print("This will delete:")
    print(f"  connector gateway: {gateway} (and all its connections + trigger configs)")
    if sandbox_group and connection:
        print(f"  gatewayConnections[] entry for '{connection}' on sandbox group '{sandbox_group}'")
    print("  trigger-related keys from .env")
    print()
    print("It will NOT delete the resource group or sandbox group.")
    if not args.yes:
        reply = input("Continue? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("aborted.")
            return

    # Remove the sandbox-group wiring FIRST, while the connection resourceId
    # is still resolvable. After gateway delete the resource is gone and the
    # SG entry would be a dangling reference.
    if sandbox_group and connection:
        connection_resource_id = (
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Web/connectorGateways/{gateway}/connections/{connection}"
        )
        print(f"==> Removing gatewayConnections entry from sandbox group '{sandbox_group}'...")
        _remove_sandboxgroup_gateway_connection(
            subscription_id, resource_group, sandbox_group, connection_resource_id,
        )

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}/providers/Microsoft.Web"
        f"/connectorGateways/{gateway}?api-version={API_VERSION}"
    )
    print(f"==> Deleting connector gateway '{gateway}'...")
    _az_rest_delete(url)
    print(f"==> Updating {ENV_FILE.name}...")
    _rewrite_env_dropping(TRIGGER_OWNED_KEYS)
    print("==> Done.")


if __name__ == "__main__":
    main()
