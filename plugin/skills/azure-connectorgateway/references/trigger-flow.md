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

```bash
az rest --method PUT \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/my-rg/providers/Microsoft.Web/connectorGateways/my-gw?api-version=2026-05-01-preview" \
  --body '{"location":"brazilsouth","identity":{"type":"SystemAssigned"}}' \
  --query "{principalId:identity.principalId, tenantId:identity.tenantId}"
```

### Step 2: Create Connection + OAuth Consent

```bash
# Create connection
az rest --method PUT \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/my-rg/providers/Microsoft.Web/connectorGateways/my-gw/connections/o365-conn?api-version=2026-05-01-preview" \
  --body '{"properties":{"api":{"name":"office365"}},"location":"brazilsouth"}'
```

Then generate consent link and open in browser — see **SKILL.md Step 3** for the exact
`listConsentLinks` body format and `Start-Process` pattern.

### Step 3: Set Up a Sandbox

```bash
# Create sandbox group (uses aca CLI from azure-sandbox skill)
aca sandboxgroup create -g my-rg -n my-sg -l eastus2

# Create sandbox
aca sandbox create -g my-rg --group my-sg --disk ubuntu
```

### Step 4: Create Trigger Config

```powershell
# Build trigger body — ShellCommand example
# NOTE: API uses connectionDetails + notificationDetails (NOT flat connectorName/operationName)
$triggerBody = @{
  properties = @{
    connectionDetails = @{
      connectorName = "office365"
      connectionName = "o365-conn"
    }
    notificationDetails = @{
      operationName = "OnNewEmailV3"
      parameters = @(
        @{ name = "folderPath"; value = "Inbox" }
      )
    }
    callbackTarget = @{
      sandboxId = "{sandbox_id}"
      sandboxGroupName = "my-sg"
      command = "python /app/handle_email.py"
    }
  }
} | ConvertTo-Json -Depth 6 -Compress

$tmpBody = New-TemporaryFile; Set-Content $tmpBody $triggerBody
az rest --method PUT `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/my-rg/providers/Microsoft.Web/connectorGateways/my-gw/triggerConfigs/email-handler?api-version=2026-05-01-preview" `
  --body "@$tmpBody"
Remove-Item $tmpBody

# For InvokePort target, replace callbackTarget with:
#   "port" = 5000; "portPath" = "/webhook"; "httpMethod" = "POST"
# For ExecuteCommand target, replace with:
#   "executeCommand" = "python"; "executeArgs" = @("/app/handle_email.py", "--verbose")
```

### Step 5: Grant Access Policy

```powershell
$aclBody = @{
  location = "brazilsouth"
  properties = @{
    principal = @{
      type = "ActiveDirectory"
      identity = @{ objectId = "{gw_principal_id}"; tenantId = "{gw_tenant_id}" }
    }
  }
} | ConvertTo-Json -Depth 5 -Compress

az rest --method PUT `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/my-rg/providers/Microsoft.Web/connectorGateways/my-gw/connections/o365-conn/accessPolicies/gateway-acl?api-version=2026-05-01-preview" `
  --body $aclBody
```

### Step 6: Discover Trigger Operations (optional)

```bash
az rest --method POST \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/my-rg/providers/Microsoft.Web/connectorGateways/my-gw/listOperations?api-version=2026-05-01-preview" \
  --body '{"connectorName":"office365"}'
# Filter for trigger operations (triggerType field present)
```

### Step 7: Manage Trigger Lifecycle

```bash
# Disable
az rest --method POST \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/my-rg/providers/Microsoft.Web/connectorGateways/my-gw/triggerConfigs/email-handler/disable?api-version=2026-05-01-preview"

# Enable
az rest --method POST \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/my-rg/providers/Microsoft.Web/connectorGateways/my-gw/triggerConfigs/email-handler/enable?api-version=2026-05-01-preview"

# Delete
az rest --method DELETE \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/my-rg/providers/Microsoft.Web/connectorGateways/my-gw/triggerConfigs/email-handler?api-version=2026-05-01-preview"
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
