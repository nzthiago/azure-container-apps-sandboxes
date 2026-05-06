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
    python trigger-getting-started.py -g <resource-group> --gateway <gw-name>
    python trigger-getting-started.py -g <resource-group> --gateway <gw-name> --connector office365
"""

import argparse
import json
import subprocess
import sys
import tempfile
import os

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
    """Call az rest and return parsed JSON."""
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
    finally:
        if body:
            os.unlink(tmp.name)


# =========================================================================
# Step 1: Discover Trigger Operations
# =========================================================================
print("\n" + "=" * 60)
print(f"Step 1: Discover Trigger Operations for {connector}")
print("=" * 60)

operations = az_rest("POST", f"{ARM_BASE}/listOperations?{API_VERSION}",
                     body={"connectorName": connector})

trigger_ops = [op for op in operations.get("value", operations) if "trigger" in op.get("operationId", "").lower() or op.get("triggerType")]
print(f"  {len(trigger_ops)} trigger operations available:")
for op in trigger_ops:
    print(f"    {op['operationId']}: {op.get('summary', '')} ({op.get('triggerType', '?')})")

selected_op = trigger_ops[0] if trigger_ops else {"operationId": "OnNewEmail"}
print(f"\n  Selected: {selected_op['operationId']}")

# =========================================================================
# Step 2: Create Trigger Config
# =========================================================================
print("\n" + "=" * 60)
print("Step 2: Create Trigger Config")
print("=" * 60)
config_name = f"{connector}-trigger"

trigger_body = {
    "properties": {
        "connectionDetails": {
            "connectorName": connector,
            "connectionName": connection_name,
        },
        "notificationDetails": {
            "operationName": selected_op["operationId"],
            "parameters": [{"name": "folderPath", "value": "Inbox"}],
        },
        "callbackTarget": {
            "sandboxId": sandbox_id,
            "sandboxGroupName": sandbox_group,
            "command": f"echo 'Trigger {config_name} fired!' >> /tmp/trigger.log",
        },
    }
}

trigger = az_rest("PUT", f"{ARM_BASE}/triggerConfigs/{config_name}?{API_VERSION}", body=trigger_body)
state = trigger.get("properties", {}).get("state", "Unknown")
print(f"  ✓ Trigger config: {config_name} ({state})")

# =========================================================================
# Step 3: List and Inspect Trigger Configs
# =========================================================================
print("\n" + "=" * 60)
print("Step 3: List Trigger Configs")
print("=" * 60)

triggers = az_rest("GET", f"{ARM_BASE}/triggerConfigs?{API_VERSION}")
for t in triggers.get("value", []):
    name = t.get("name", "?")
    s = t.get("properties", {}).get("state", "?")
    print(f"    {name}: {s}")

# =========================================================================
# Step 4: Enable / Disable
# =========================================================================
print("\n" + "=" * 60)
print("Step 4: Enable / Disable Trigger Config")
print("=" * 60)

az_rest("POST", f"{ARM_BASE}/triggerConfigs/{config_name}/disable?{API_VERSION}")
print(f"  Disabled: {config_name}")

az_rest("POST", f"{ARM_BASE}/triggerConfigs/{config_name}/enable?{API_VERSION}")
print(f"  Enabled: {config_name}")

# =========================================================================
# Step 5: Cleanup
# =========================================================================
if args.cleanup:
    print("\n" + "=" * 60)
    print("Step 5: Cleanup")
    print("=" * 60)
    az_rest("DELETE", f"{ARM_BASE}/triggerConfigs/{config_name}?{API_VERSION}")
    print(f"  ✓ Deleted trigger config: {config_name}")
else:
    print(f"\n  Trigger left running. Pass --cleanup to delete.")

print("\nDone!")

