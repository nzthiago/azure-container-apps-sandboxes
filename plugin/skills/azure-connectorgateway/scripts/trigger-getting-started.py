"""Trigger Getting Started — Create and manage trigger configs using az rest.

End-to-end walk-through:
  1. Discover available trigger operations
  2. Create a trigger config (connector event → sandbox command)
  3. List and inspect trigger configs
  4. Enable / disable the trigger config
  5. Delete the trigger config

Prerequisites:
  - Azure CLI signed in (az login)
  - Connector gateway + connection already set up
  - Sandbox group + sandbox already created

Usage:
    python trigger-getting-started.py -g <resource-group> --gateway <gw-name> --sandbox-id <id> --sandbox-group <sg>
    python trigger-getting-started.py -g <resource-group> --gateway <gw-name> --sandbox-id <id> --sandbox-group <sg> --connector office365
"""

import argparse
import json
import subprocess
import sys
import tempfile
import os
import uuid

parser = argparse.ArgumentParser(description="Trigger Getting Started")
parser.add_argument("-g", "--resource-group", required=True, help="Resource group")
parser.add_argument("--gateway", required=True, help="Connector gateway name")
parser.add_argument("--connector", default="office365", help="Connector type (default: office365)")
parser.add_argument("--connection-name", default=None, help="Connection name on the gateway")
parser.add_argument("--sandbox-id", required=True, help="Sandbox ID for trigger target")
parser.add_argument("--sandbox-group", required=True, help="Sandbox group for trigger target")
parser.add_argument("--cleanup", action="store_true", help="Delete trigger after")
args = parser.parse_args()

account = json.loads(subprocess.run(
    ["az", "account", "show", "-o", "json"],
    capture_output=True, text=True, check=True).stdout)

subscription_id = account["id"]
rg = args.resource_group
gateway = args.gateway
connector = args.connector
connection_name = args.connection_name or f"{connector}-conn"
sandbox_id = args.sandbox_id
sandbox_group = args.sandbox_group

ARM_BASE = f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gateway}"
API_VERSION = "api-version=2026-05-01-preview"

print(f"User:             {account['user']['name']}")
print(f"Subscription:     {account['name']} ({subscription_id})")
print(f"Resource Group:   {rg}")
print(f"Gateway:          {gateway}")
print(f"Connector:        {connector}")
print(f"Connection:       {connection_name}")


def az_rest(method, url, body=None):
    """Call az rest and return parsed JSON. Exits with helpful message on failure."""
    cmd = ["az", "rest", "--method", method, "--url", url]
    if body:
        # Use temp file to avoid PowerShell/shell quoting issues
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(body, tmp)
        tmp.close()
        cmd += ["--body", f"@{tmp.name}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr[:300] if e.stderr else "unknown error"
        print(f"  ❌ az rest {method} failed: {error_msg}")
        raise
    finally:
        if body:
            os.unlink(tmp.name)


# =========================================================================
# Step 1: Discover Trigger Operations
# =========================================================================
print("\n" + "=" * 60)
print(f"Step 1: Discover Trigger Operations for {connector}")
print("=" * 60)

# Use the classic locations API to discover operations
# First get gateway location
gw_info = az_rest("GET", f"{ARM_BASE}?{API_VERSION}")
location = gw_info.get("location", "westcentralus")

ops_url = f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.Web/locations/{location}/managedApis/{connector}/apiOperations?api-version=2016-06-01"
operations = az_rest("GET", ops_url)

raw_ops = operations.get("value", []) if isinstance(operations, dict) else operations
trigger_ops = [op for op in raw_ops if op.get("properties", {}).get("trigger")]

if not trigger_ops:
    print("  ❌ No trigger operations found for this connector.")
    print(f"     Verify connector name '{connector}' is correct and available in location '{location}'.")
    sys.exit(1)

print(f"  {len(trigger_ops)} trigger operations available:")
for i, op in enumerate(trigger_ops, 1):
    props = op.get("properties", {})
    trigger_type = props.get("trigger", "")
    print(f"    {i}. {op['name']}: {props.get('summary', '')} [{trigger_type}]")

# Auto-select first trigger for demo purposes (interactive scripts should prompt user)
selected_op = trigger_ops[0]
selected_name = selected_op.get("name", selected_op.get("operationId"))
trigger_type = selected_op.get("properties", {}).get("trigger", "")
print(f"\n  Selected: {selected_name}")

# Polling/recurrence trigger detection:
# If the trigger operation does NOT have x-ms-notification AND does NOT have
# x-ms-notification-content in its Swagger definition, it is a recurrence trigger.
# Only in that case, inform the user about the default polling interval.
if trigger_type in ("batch", "Batch", "single", "Single"):
    # NOTE: To determine if this is a recurrence trigger, check the Swagger for
    # the operation. If it lacks both x-ms-notification and x-ms-notification-content,
    # it's a recurrence/polling trigger regardless of single/batch.
    print(f"\n  ℹ️  Check Swagger for this operation to determine if it's a recurrence trigger.")
    print(f"      If no x-ms-notification and no x-ms-notification-content → it's a polling trigger.")
    print(f"      Default recurrence: every 3 minutes. Ask user if they want to change it.")

# =========================================================================
# Step 2: Create Access Policy + RBAC (required before trigger creation)
# =========================================================================
print("\n" + "=" * 60)
print("Step 2: Create Access Policy + RBAC")
print("=" * 60)

# Get gateway identity
gw_principal_id = gw_info.get("identity", {}).get("principalId")
gw_tenant_id = gw_info.get("identity", {}).get("tenantId")
if not gw_principal_id:
    print("  ❌ Gateway has no managed identity. Cannot create trigger.")
    print("     Recreate gateway with SystemAssigned identity.")
    sys.exit(1)

# Create access policy (gateway MI → connection)
acl_body = {
    "location": location,
    "properties": {
        "principal": {
            "type": "ActiveDirectory",
            "identity": {"objectId": gw_principal_id, "tenantId": gw_tenant_id},
        }
    },
}
az_rest("PUT", f"{ARM_BASE}/connections/{connection_name}/accessPolicies/gateway-acl?{API_VERSION}", body=acl_body)
print(f"  ✓ Access policy: gateway MI → {connection_name}")

# Assign RBAC role (Dev Compute SandboxGroup Data Owner) on sandbox group
ROLE_ID = "c24cf47c-5077-412d-a19c-45202126392c"
sg_scope = f"/subscriptions/{subscription_id}/resourceGroups/{rg}/providers/Microsoft.App/sandboxGroups/{sandbox_group}"
role_body = {
    "properties": {
        "roleDefinitionId": f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleDefinitions/{ROLE_ID}",
        "principalId": gw_principal_id,
        "principalType": "ServicePrincipal",
    }
}
assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{gw_principal_id}-{sandbox_group}-{ROLE_ID}"))
role_url = f"https://management.azure.com{sg_scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_name}?api-version=2022-04-01"
try:
    az_rest("PUT", role_url, body=role_body)
    print(f"  ✓ RBAC role assigned: gateway MI → sandbox group")
except subprocess.CalledProcessError as e:
    if "RoleAssignmentExists" in (e.stderr or ""):
        print(f"  ✓ RBAC role already exists")
    else:
        print(f"  ⚠️  RBAC assignment failed (trigger may 403): {e.stderr[:200] if e.stderr else 'unknown error'}")

# =========================================================================
# Step 3: Create Trigger Config
# =========================================================================
print("\n" + "=" * 60)
print("Step 3: Create Trigger Config")
print("=" * 60)
config_name = f"{connector}-trigger"

# Build connector-aware default parameters
DEFAULT_PARAMS = {
    "office365": [{"name": "folderPath", "value": "Inbox"}],
    "sharepointonline": [],  # requires dynamic values (siteUrl, listName)
    "onedriveforbusiness": [],  # requires dynamic values (folderPath)
    "teams": [],  # requires dynamic values (teamId, channelId)
}
parameters = DEFAULT_PARAMS.get(connector, [])
if not parameters:
    print(f"  ℹ️  No default parameters for '{connector}'. Trigger will use operation defaults.")

trigger_body = {
    "properties": {
        "connectionDetails": {
            "connectorName": connector,
            "connectionName": connection_name,
        },
        "metadata": {
            "sandboxGroupName": sandbox_group,
            "sandboxId": sandbox_id,
        },
        "notificationDetails": {
            "authentication": {
                "audience": "https://management.azuredevcompute.io/",
                "type": "ManagedServiceIdentity",
            },
            "body": {
                "activationMode": "OnDemand",
                "command": f"echo 'Trigger {config_name} fired!' >> /tmp/trigger.log",
            },
            "callbackUrl": f"https://management.azuredevcompute.io/subscriptions/{subscription_id}/resourceGroups/{rg}/sandboxGroups/{sandbox_group}/sandboxes/{sandbox_id}/executeShellCommand?api-version=2026-02-01-preview",
            "httpMethod": "Post",
        },
        "operationName": selected_name,
        "parameters": parameters,
    }
}

trigger = az_rest("PUT", f"{ARM_BASE}/triggerConfigs/{config_name}?{API_VERSION}", body=trigger_body)
state = trigger.get("properties", {}).get("state", "Unknown")
print(f"  ✓ Trigger config: {config_name} ({state})")

# =========================================================================
# Step 4: List and Inspect Trigger Configs
# =========================================================================
print("\n" + "=" * 60)
print("Step 4: List Trigger Configs")
print("=" * 60)

triggers = az_rest("GET", f"{ARM_BASE}/triggerConfigs?{API_VERSION}")
for t in triggers.get("value", []):
    name = t.get("name", "?")
    s = t.get("properties", {}).get("state", "?")
    print(f"    {name}: {s}")

# =========================================================================
# Step 5: Enable / Disable
# =========================================================================
print("\n" + "=" * 60)
print("Step 5: Enable / Disable Trigger Config")
print("=" * 60)

az_rest("POST", f"{ARM_BASE}/triggerConfigs/{config_name}/disable?{API_VERSION}")
print(f"  Disabled: {config_name}")

az_rest("POST", f"{ARM_BASE}/triggerConfigs/{config_name}/enable?{API_VERSION}")
print(f"  Enabled: {config_name}")

# =========================================================================
# Step 6: Cleanup
# =========================================================================
if args.cleanup:
    print("\n" + "=" * 60)
    print("Step 6: Cleanup")
    print("=" * 60)
    az_rest("DELETE", f"{ARM_BASE}/triggerConfigs/{config_name}?{API_VERSION}")
    print(f"  ✓ Deleted trigger config: {config_name}")
else:
    print(f"\n  Trigger left running. Pass --cleanup to delete.")

print("\nDone!")

