"""Trigger Getting Started — Create and manage trigger configs.

End-to-end walk-through:
  1. Connect to Azure and initialize clients
  2. Discover available trigger operations from managed API Swagger
  3. Create a trigger config (connector event → sandbox command)
  4. List and inspect trigger configs
  5. Enable / disable the trigger config
  6. Delete the trigger config

Prerequisites:
  - Azure CLI signed in (az login)
  - Connector gateway + connection already set up
  - Sandbox group + sandbox already created
  - pip install azure-connectorgateway

Usage:
    python trigger-getting-started.py -g <resource-group> --gateway <gw-name>
    python trigger-getting-started.py -g <resource-group> --gateway <gw-name> --connector office365
"""

import argparse
import json
import subprocess

parser = argparse.ArgumentParser(description="Trigger Getting Started")
parser.add_argument("-g", "--resource-group", default=None, help="Resource group")
parser.add_argument("--gateway", required=True, help="Connector gateway name")
parser.add_argument("--connector", default="office365", help="Connector type (default: office365)")
parser.add_argument("--connection-name", default=None, help="Connection name on the gateway")
parser.add_argument("--sandbox-id", default=None, help="Sandbox ID for trigger target")
parser.add_argument("--sandbox-group", default=None, help="Sandbox group for trigger target")
parser.add_argument("--cleanup", action="store_true", help="Delete trigger after")
args = parser.parse_args()

account = json.loads(subprocess.run(
    ["az", "account", "show", "-o", "json"],
    capture_output=True, text=True, check=True).stdout)

subscription_id = account["id"]
rg = args.resource_group or "trigger-lab-rg"
gateway = args.gateway
connector = args.connector
connection_name = args.connection_name or f"{connector}-conn"

print(f"User:             {account['user']['name']}")
print(f"Subscription:     {account['name']} ({subscription_id})")
print(f"Resource Group:   {rg}")
print(f"Gateway:          {gateway}")
print(f"Connector:        {connector}")
print(f"Connection:       {connection_name}")

from azure.connectorgateway import TriggerClient

client = TriggerClient(subscription_id=subscription_id, resource_group=rg)

# =========================================================================
# Step 1: Discover Trigger Operations
# =========================================================================
print("\n" + "=" * 60)
print(f"Step 1: Discover Trigger Operations for {connector}")
print("=" * 60)
try:
    operations = client.list_trigger_operations(gateway, connector)
    print(f"  {len(operations)} trigger operations available:")
    for op in operations:
        print(f"    {op['operationId']}: {op['summary']} ({op.get('triggerType', '?')})")
        for p in op.get("parameters", []):
            req = "required" if p.get("required") else "optional"
            print(f"      param: {p['name']} ({p.get('type', 'string')}, {req})")
except Exception as e:
    print(f"  Could not discover operations: {e}")
    print("  Using default: OnNewEmail")
    operations = [{"operationId": "OnNewEmail", "summary": "When a new email arrives"}]

selected_op = operations[0]
print(f"\n  Selected: {selected_op['operationId']} — {selected_op.get('summary', '')}")

# =========================================================================
# Step 2: Create Trigger Config
# =========================================================================
print("\n" + "=" * 60)
print("Step 2: Create Trigger Config")
print("=" * 60)
config_name = f"{connector}-trigger"
sandbox_id = args.sandbox_id or "my-sandbox-id"
sandbox_group = args.sandbox_group or "my-sandbox-group"

try:
    trigger = client.create_trigger(gateway, config_name,
        connector_name=connector,
        connection_name=connection_name,
        operation_name=selected_op["operationId"],
        sandbox_id=sandbox_id,
        sandbox_group=sandbox_group,
        command=f"echo 'Trigger {config_name} fired!' >> /tmp/trigger.log",
        parameters=[{"name": "folderPath", "value": "Inbox"}],
    )
    state = trigger["properties"]["state"]
    callback = trigger["properties"]["notificationDetails"]["callbackUrl"]
    print(f"  ✓ Trigger config: {config_name} ({state})")
    print(f"    Callback: {callback}")
except Exception as e:
    print(f"  Error creating trigger config: {e}")
    raise SystemExit(1)

# =========================================================================
# Step 3: List and Inspect Trigger Configs
# =========================================================================
print("\n" + "=" * 60)
print("Step 3: List Trigger Configs")
print("=" * 60)
triggers = client.list_triggers(gateway)
print(f"  {len(triggers)} trigger config(s):")
for t in triggers:
    name = t.get("name", "?")
    state = t.get("properties", {}).get("state", "?")
    op = t.get("properties", {}).get("operationName", "?")
    print(f"    {name}: {state} (operation={op})")

# =========================================================================
# Step 4: Enable / Disable
# =========================================================================
print("\n" + "=" * 60)
print("Step 4: Enable / Disable Trigger Config")
print("=" * 60)
result = client.disable_trigger(gateway, config_name)
print(f"  Disabled: {result['properties']['state']}")

result = client.enable_trigger(gateway, config_name)
print(f"  Enabled: {result['properties']['state']}")

# =========================================================================
# Step 5: Cleanup
# =========================================================================
if args.cleanup:
    print("\n" + "=" * 60)
    print("Step 5: Cleanup")
    print("=" * 60)
    client.delete_trigger(gateway, config_name)
    print(f"  ✓ Deleted trigger config: {config_name}")
else:
    print(f"\n  Trigger left running. Pass --cleanup to delete.")

print("\nDone!")
