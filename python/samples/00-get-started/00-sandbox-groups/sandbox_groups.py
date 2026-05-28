"""Sandbox group lifecycle - create a group, assign role, run sandbox, clean up.

Walks through the full provisioning flow end-to-end so you see every
step that's normally hidden behind ``samples/sandboxes/setup/python/setup.py``:

  1. Create a sandbox group         (ARM control plane)
  2. Assign 'Container Apps SandboxGroup Data Owner' to the current
     principal at the GROUP scope                                (RBAC)
  3. Create a sandbox in that group, run a command, delete it    (data plane)
  4. Delete the sandbox group                                    (cleanup)

This guide creates its OWN throwaway group (``guide-00-<short-uuid>``)
so it doesn't collide with the shared ``ai-apps-samples-group`` used by
the other guides. It only requires an existing resource group and a
region (read from ``samples/.env``).

The SDK transparently retries 403s during role propagation for ~100s,
so no manual sleep is needed between role assignment and the first
data-plane call.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import uuid
from pathlib import Path

from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.containerapps.sandbox import (
    SandboxGroupClient,
    SandboxGroupManagementClient,
    endpoint_for_region,
)

ROLE_NAME = "Container Apps SandboxGroup Data Owner"


def _load_env() -> None:
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


def _principal() -> tuple[str, str]:
    """Return (oid, principal_type) from the JWT 'oid' claim.

    Works for both users and service principals; no Graph permission
    required.
    """
    token = DefaultAzureCredential().get_token(
        "https://management.azure.com/.default"
    )
    payload = token.token.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    oid = claims["oid"]
    idtyp = (claims.get("idtyp") or "").lower()
    if idtyp == "app":
        return oid, "ServicePrincipal"
    if idtyp == "user" or claims.get("upn") or claims.get("preferred_username"):
        return oid, "User"
    return oid, "ServicePrincipal"


def main() -> None:
    _load_env()
    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    resource_group = os.environ["ACA_RESOURCE_GROUP"]
    region = os.environ["ACA_SANDBOXGROUP_REGION"]

    # Throwaway group name unique to this run so we never collide with
    # the shared group that setup.py provisioned.
    group_name = f"guide-00-{uuid.uuid4().hex[:8]}"
    group_scope = (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.App/sandboxGroups/{group_name}"
    )

    print(f"==> Subscription:   {subscription_id}")
    print(f"    Resource group: {resource_group}")
    print(f"    Region:         {region}")
    print(f"    Sandbox group:  {group_name}  (will be deleted at end)")

    credential = DefaultAzureCredential()
    mgmt = SandboxGroupManagementClient(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
    )
    auth = AuthorizationManagementClient(credential, subscription_id)
    data: SandboxGroupClient | None = None

    group_created = False
    try:
        # ----- 1. Create the sandbox group (ARM control plane) -----
        print(f"==> Creating sandbox group '{group_name}' in {region}...")
        mgmt.create_group(group_name, location=region)
        group_created = True

        # ----- 1a. List groups in this resource group -----
        print(f"==> Listing sandbox groups in '{resource_group}':")
        for g in mgmt.list_groups():
            marker = " <-- just created" if g.name == group_name else ""
            print(f"    - {g.name} ({g.location}){marker}")

        # ----- 1b. Get full details for our new group -----
        print(f"==> Getting details for '{group_name}':")
        detail = mgmt.get_group(group_name)
        print(f"    location:   {detail.location}")
        print(f"    state:      {detail.properties.get('provisioningState', '?')}")
        print(f"    endpoint:   {detail.properties.get('managementEndpoint', '?')}")

        # ----- 2. Assign the data-owner role at GROUP scope -----
        principal_id, principal_type = _principal()
        print(f"==> Assigning '{ROLE_NAME}'")
        print(f"    to {principal_type} {principal_id}")
        print(f"    at scope: ../sandboxGroups/{group_name}")
        role_def = next(
            auth.role_definitions.list(group_scope, filter=f"roleName eq '{ROLE_NAME}'"),
            None,
        )
        if role_def is None:
            sys.exit(f"error: role '{ROLE_NAME}' not found")
        try:
            auth.role_assignments.create(
                group_scope,
                str(uuid.uuid4()),
                {
                    "role_definition_id": role_def.id,
                    "principal_id": principal_id,
                    "principal_type": principal_type,
                },
            )
        except HttpResponseError as exc:
            if "RoleAssignmentExists" not in str(exc) and "Conflict" not in str(exc):
                raise
            print("    (role assignment already exists, continuing)")

        # ----- 3. Use the data plane (SDK auto-retries 403s for ~100s) -----
        print("==> Creating sandbox in the new group...")
        data = SandboxGroupClient(
            endpoint_for_region(region),
            credential,
            subscription_id=subscription_id,
            resource_group=resource_group,
            sandbox_group=group_name,
        )
        sandbox = data.begin_create_sandbox(disk="ubuntu").result()
        print(f"    sandbox: {sandbox.sandbox_id}")

        print("==> Running command in sandbox...")
        result = sandbox.exec("echo hello from $(hostname)")
        if result.stdout:
            sys.stdout.write(result.stdout)
            if not result.stdout.endswith("\n"):
                sys.stdout.write("\n")
        if result.exit_code != 0:
            sys.exit(f"command exited with code {result.exit_code}")

        print(f"==> Deleting sandbox {sandbox.sandbox_id}...")
        sandbox.delete()
    finally:
        if data is not None:
            data.close()
        if group_created:
            print(f"==> Deleting sandbox group '{group_name}'...")
            try:
                mgmt.delete_group(group_name)
            except HttpResponseError as exc:
                print(f"    warning: group delete failed: {exc}")
        mgmt.close()
        auth.close()
        credential.close()

    print("==> Done.")


if __name__ == "__main__":
    main()
