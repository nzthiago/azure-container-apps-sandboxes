# Trigger Flow — How It All Wires Up

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  External Event                                                  │
│  (email received, file created, webhook fired)                   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Connector Gateway (ARM: Microsoft.Web/connectorGateways)        │
│  ├── Connection: OAuth-authorized access to the connector        │
│  ├── Trigger Config: event subscription + callback delivery      │
│  └── Access Policy: gateway MI granted access to connection      │
│                                                                  │
│  Trigger Config is the MAIN trigger resource. It:                │
│  1. Subscribes to connector events via the connection            │
│  2. Authenticates to sandbox using gateway MI                    │
│  3. Delivers event payload to the sandbox callback URL           │
└────────────────────────┬────────────────────────────────────────┘
                         │ callback (POST to sandbox)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Sandbox Target                                                  │
│  ShellCommand: {ADC}/.../{sandboxId}/executeShellCommand         │
│  InvokePort:   https://{sandboxId}--{port}.proxy.azuredevcompute.io │
│  Runs inside KVM µVM with hardware isolation                     │
└─────────────────────────────────────────────────────────────────┘
```

## End-to-End Flow

### Step 1: Create Connector Gateway with SystemAssigned Identity

```python
from azure.connectorgateway import ConnectorGatewayClient

conn_client = ConnectorGatewayClient(resource_group="my-rg")
gw = conn_client.create_gateway("my-gw", location="brazilsouth",
    identity={"type": "SystemAssigned"})
gw_principal_id = gw["identity"]["principalId"]
gw_tenant_id = gw["identity"]["tenantId"]
```

### Step 2: Create Connection + OAuth Consent

```python
conn = conn_client.create_connection("my-gw", "o365-conn",
    connector_name="office365")
link = conn_client.generate_consent_link("my-gw", "o365-conn")
# Open link → authorize → confirm consent
```

### Step 3: Set Up a Sandbox

```python
from azure.sandbox import SandboxClient
from azure.mgmt.sandbox import SandboxGroupManagementClient

mgmt = SandboxGroupManagementClient(resource_group="my-rg")
mgmt.create_group("my-sg", location="eastus2")

sbx_client = SandboxClient(resource_group="my-rg")
sbx = sbx_client.create_sandbox("my-sg", disk="ubuntu")
sandbox_id = sbx["id"]
```

### Step 4: Create Trigger Config

```python
from azure.connectorgateway import TriggerClient

trigger_client = TriggerClient(resource_group="my-rg")

# ShellCommand target (shell-interpreted command string)
trigger = trigger_client.create_trigger("my-gw", "email-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id=sandbox_id,
    sandbox_group="my-sg",
    command="python /app/handle_email.py",
    parameters=[{"name": "folderPath", "value": "Inbox"}])

# ExecuteCommand target (direct exec, no shell)
trigger = trigger_client.create_trigger("my-gw", "cmd-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id=sandbox_id,
    sandbox_group="my-sg",
    execute_command="python",
    execute_args=["/app/handle_email.py", "--verbose"],
    parameters=[{"name": "folderPath", "value": "Inbox"}])

# InvokePort target (HTTP call to sandbox port)
trigger = trigger_client.create_trigger("my-gw", "webhook-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id=sandbox_id,
    sandbox_group="my-sg",
    port=5000,
    port_path="/webhook",
    parameters=[{"name": "folderPath", "value": "Inbox"}])
```

### Step 5: Grant Access Policy

```python
conn_client.create_access_policy("my-gw", "o365-conn",
    principal_id=gw_principal_id,
    tenant_id=gw_tenant_id,
    location="brazilsouth")
```

### Step 6: Discover Trigger Operations (optional)

```python
ops = trigger_client.list_trigger_operations("my-gw", "office365")
for op in ops:
    print(f"  {op['operationId']}: {op['summary']} ({op['triggerType']})")
```

### Step 7: Manage Trigger Lifecycle

```python
trigger_client.disable_trigger("my-gw", "email-handler")
trigger_client.enable_trigger("my-gw", "email-handler")
trigger_client.delete_trigger("my-gw", "email-handler")
```

## Trigger Source Parameters

| Connector | Operation | Key Parameters |
|-----------|-----------|---------------|
| Office 365 | OnNewEmail | `folderPath` (Inbox, Sent Items, Drafts) |
| Office 365 | OnNewFileV2 | `folderId` (OneDrive folder) |
| GitHub | OnPush | `repository`, `branch` |
| Azure Blob | OnBlobCreated | `containerName`, `path` |

## Target Types

| Type | Callback URL Pattern | Use Case |
|------|---------------------|----------|
| ShellCommand | `{ADC}/subscriptions/{sub}/resourceGroups/{rg}/sandboxGroups/{sg}/sandboxes/{id}/executeShellCommand` | Run a shell-interpreted command string |
| ExecuteCommand | `{ADC}/subscriptions/{sub}/resourceGroups/{rg}/sandboxGroups/{sg}/sandboxes/{id}/executeCommand` | Run a command directly (no shell, explicit args) |
| InvokePort | `https://{sandboxId}--{port}.proxy.azuredevcompute.io/{path}` | HTTP call to a sandbox port |

### ShellCommand body fields
- `command` — the shell command to execute (interpreted by shell)
- `shell` — shell to use (e.g., `/bin/bash`)
- `workingDirectory` — cwd for the command
- `environmentVariables` — env vars to set
- `activationMode` — `OnDemand` (auto-starts sandbox if stopped)

### ExecuteCommand body fields
- `command` — the binary/executable to run (no shell interpretation)
- `args` — array of arguments passed to the command
- `workingDirectory` — cwd for the command
- `environmentVariables` — env vars to set
- `activationMode` — `OnDemand` (auto-starts sandbox if stopped)

### InvokePort authentication
For InvokePort targets, the gateway MI must be added to the port's Entra ID
objectIds to authenticate. The proxy URL uses `audience: https://auth.adcproxy.io/`.

### ShellCommand vs ExecuteCommand

| | ShellCommand | ExecuteCommand |
|--|-------------|----------------|
| Shell interpretation | Yes (`/bin/sh -c "..."`) | No (direct exec) |
| Pipes & redirects | Supported | Not supported |
| Env var expansion | Shell-level (`$VAR`) | Only via `environmentVariables` |
| Safety | Command string is shell-interpreted | Safer for untrusted input |
| SDK param | `command="python /app/main.py"` | `execute_command="python", execute_args=["/app/main.py"]` |

## Gotchas

| Issue | Solution |
|-------|----------|
| Trigger not firing | Ensure access policy is created granting gateway MI access to connection |
| Gateway can't subscribe | Create an access policy for the gateway MI on the connection |
| Sandbox not responding | Ensure sandbox is Running; for ShellCommand, use `activationMode: OnDemand` |
| Port auth failure | Add gateway principalId to port's `auth.entraId.objectIds` on the sandbox |
| Parameters rejected | Use `list_trigger_operations` to get exact parameter names from Swagger |
| Cleanup order | Delete trigger config → sandbox → gateway (gateway deletion cascades connections) |
