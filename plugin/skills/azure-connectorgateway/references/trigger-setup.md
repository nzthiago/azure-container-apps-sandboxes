# Trigger Setup (Steps 5B–9B)

Detailed commands for creating event-driven triggers on a connector gateway.

## Step 5B: Discover trigger operations

```bash
az rest --method POST \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/listOperations?api-version=2026-05-01-preview" \
  --body '{"connectorName":"office365"}'
# Filter response for trigger operations (type contains "trigger")
```
Present operations as choices (summary + operationId). Let user pick.

## Step 6B: Collect trigger parameters

For each required parameter:
- **Dynamic values** (`x-ms-dynamic-values`, `x-ms-dynamic-list`, `x-ms-dynamic-tree`):
  fetch from API, present as choices. See [dynamic-values.md](dynamic-values.md).
- **Static enum**: present values as choices.
- **Free-form with obvious default** (e.g., `folderPath=Inbox`):
  use default BUT inform user.
- **Free-form, no obvious default**: ask user.

Common examples:
- Email trigger: `folderPath` → default `Inbox` (inform user), `subjectFilter` → optional (skip)
- SharePoint trigger: `siteUrl` → dynamic list, `listName` → dynamic list
- OneDrive trigger: `folderPath` → dynamic tree

Build parameters:
```python
parameters = [
    {'name': 'folderPath', 'value': 'Inbox'},
    {'name': 'subjectFilter', 'value': 'Feedback'},
]
```

## Step 7B: Sandbox target

Ask user for existing sandbox or create new:
```bash
# List existing groups (prefer reuse — new groups take 5-20 min to propagate)
aca sandboxgroup list -g {rg}

# Create new group if needed
aca sandboxgroup create -g {rg} -n {sg} -l {location}

# Create sandbox (retry with backoff if SandboxGroupNotFound)
aca sandbox create -g {rg} --group {sg} --disk ubuntu

# Wait for Running state
aca sandbox show -g {rg} --group {sg} --id {sandbox_id} --query "state"
```

> **⚠️ Identity (principalId) is on the sandbox GROUP, not individual sandboxes.**
> Get it: `aca sandboxgroup show -g {rg} -n {sg} --query "identity.principalId"`

Ask for callback type:
- **ShellCommand** — `python /app/handler.py` (shell-interpreted)
- **ExecuteCommand** — `python` with args (no shell)
- **InvokePort** — POST to port 5000, path `/webhook`

## Step 8B: Create trigger + access policy + role assignment

> **⚡ These three operations are independent — run in parallel.**

### Trigger creation

> **⚠️ The trigger API uses `connectionDetails` + `notificationDetails`, NOT `connectorName` + `connectionName` at top level.**
> The SDK's `create_trigger()` sends a `metadata` field that the API rejects. Use `az rest` with the exact body below.

```powershell
# Build trigger config body — ShellCommand example
$triggerBody = @{
  properties = @{
    connectionDetails = @{
      connectorName = "office365"
      connectionName = "{conn}"
    }
    notificationDetails = @{
      operationName = "OnNewEmailV3"
      parameters = @(
        @{ name = "folderPath"; value = "Inbox" }
      )
    }
    callbackTarget = @{
      sandboxId = "{sandbox_id}"
      sandboxGroupName = "{sandbox_group}"
      command = "python /app/handler.py"
    }
  }
} | ConvertTo-Json -Depth 6 -Compress

# For ExecuteCommand target — replace command with:
#   "executeCommand" = "python"; "executeArgs" = @("/app/handler.py", "--verbose")
# For InvokePort target — replace command with:
#   "port" = 5000; "portPath" = "/webhook"; "httpMethod" = "POST"

$tmpBody = New-TemporaryFile; Set-Content $tmpBody $triggerBody
az rest --method PUT `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/triggerConfigs/{trigger_name}?api-version=2026-05-01-preview" `
  --body "@$tmpBody"
Remove-Item $tmpBody
```

> **⚠️ Always use `@$tmpFile` pattern for `az rest --body`** — inline JSON strings
> cause "Unsupported Media Type" errors due to PowerShell string quoting issues.

### Access policy (gateway MI → connection)

```powershell
$body = @{
  location = "{location}"
  properties = @{
    principal = @{
      type = "ActiveDirectory"
      identity = @{ objectId = "{gw_principal_id}"; tenantId = "{tenant_id}" }
    }
  }
} | ConvertTo-Json -Depth 5 -Compress

az rest --method PUT `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/accessPolicies/gateway-acl?api-version=2026-05-01-preview" `
  --body $body
```

### Port auth (InvokePort only)

```bash
aca sandbox port add -g {rg} --group {sandbox_group} --id {sandbox_id} --port 5000 \
  --entra-id-object-ids {gw_principal_id}
```

### Role assignment (ShellCommand/ExecuteCommand only)

Grant gateway MI the **"Dev Compute SandboxGroup Data Owner"** role:
```bash
az role assignment create \
  --assignee-object-id {gw_principal_id} \
  --assignee-principal-type ServicePrincipal \
  --role "c24cf47c-5077-412d-a19c-45202126392c" \
  --scope "/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.App/sandboxGroups/{sg}"
```
> **⚠️ Do NOT use Contributor.** Use the scoped data plane role above.

## Step 9B: Verify trigger

```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/triggerConfigs/{trigger}?api-version=2026-05-01-preview" \
  --query "properties.state" -o tsv
# Should output: Enabled
```
