"""Managed identity — create a sandbox group with SystemAssigned identity, then tear it down.

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import SandboxGroupManagementClient


def _load_env() -> None:
    """Load samples/.env; exit with a friendly error if it isn't there yet."""
    import sys
    for parent in Path(__file__).resolve().parents:
        env = parent / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            break
    if not os.environ.get("ACA_SANDBOXGROUP_REGION"):
        sys.exit(
            "error: samples/.env is missing required keys. Run:\n"
            "       python samples/sandboxes/setup/python/setup.py"
        )


def main() -> None:
    _load_env()
    credential = DefaultAzureCredential()
    mgmt = SandboxGroupManagementClient(
        credential,
        subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
        resource_group=os.environ["ACA_RESOURCE_GROUP"],
    )

    region = os.environ["ACA_SANDBOXGROUP_REGION"]
    name = f"mi-demo-{uuid.uuid4().hex[:6]}"

    try:
        print(f"==> begin_create_group({name!r}, identity=SystemAssigned)...")
        group = mgmt.begin_create_group(
            name, region, identity={"type": "SystemAssigned"},
        ).result()
        print(f"    created: {group.name}")
        ident = group.identity or {}
        print(f"    identity.type        = {ident.get('type')}")
        print(f"    identity.principalId = {ident.get('principalId')}")
        print(f"    identity.tenantId    = {ident.get('tenantId')}")

        if not ident.get("principalId"):
            print("\n    (the principalId may take a few seconds to appear; re-reading...)")
            time.sleep(5)
            group = mgmt.get_group(name)
            ident = group.identity or {}
            print(f"    identity.principalId = {ident.get('principalId')}")

        print("\n==> What you'd do next in a real swarm scenario:")
        print("    - Assign 'Container Apps SandboxGroup Data Owner' to this")
        print("      principalId on a different 'worker' sandbox group.")
        print("    - Boot a sandbox in THIS group with the SDK / CLI.")
        print("    - From inside that sandbox, use ManagedIdentityCredential()")
        print("      to create sandboxes in the worker group with no secrets.")
        print("    See: samples/sandboxes/scenarios/04-swarms (placeholder, coming soon).")

        print("\n==> patch_group_identity — remove the identity...")
        mgmt.patch_group_identity(name, {"type": "None"})
        group = mgmt.get_group(name)
        ident = group.identity or {}
        print(f"    identity.type = {ident.get('type', '<none>')}")
    finally:
        print(f"\n==> Deleting temp group {name}...")
        try:
            mgmt.delete_group(name)
        except Exception as exc:
            print(f"    cleanup warning: {exc}")
        mgmt.close()
        credential.close()


if __name__ == "__main__":
    main()
