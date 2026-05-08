# Trigger Setup (Steps 5B–9B)

Detailed commands for creating event-driven triggers on a connector gateway.

## Step 5B: Discover trigger operations

```bash
# Discover operations for the connector
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Web/locations/{location}/managedApis/office365/apiOperations?api-version=2016-06-01"
# Filter: trigger operations have non-empty "properties.trigger" field
```
Present operations as choices (summary + operationId). Let user pick.

> **⚠️ Identifying recurrence/polling triggers:** Check the Swagger definition for the selected operation.
> If the operation does **NOT** have `x-ms-notification` AND does **NOT** have `x-ms-notification-content`,
> it is a **recurrence/polling trigger** (this applies to both `single` and `batch` trigger types).
>
> Only for recurrence triggers: inform the user — "This trigger polls every 3 minutes by default.
> Would you like a different interval?" If yes, add a `recurrence` parameter
> (e.g., `{"name": "recurrence", "value": {"frequency": "Minute", "interval": 15}}`).
>
> If the operation HAS `x-ms-notification` or `x-ms-notification-content`, it is a
> notification/webhook trigger — it fires on events and does NOT poll. Do not mention recurrence.

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

Ask the user: "Do you have an existing sandbox group, or should I create a new one?"

**If existing:** List available groups and let the user pick:
```bash
aca sandboxgroup list -g {rg} --query "[].{name:name, location:location, identity:identity.type}" -o table
```
Present the list. After the user selects, verify it has a managed identity:
```bash
aca sandboxgroup show -g {rg} -n {sg} --query "identity.principalId" -o tsv
```
If `principalId` is empty/null, enable MI:
```bash
aca sandboxgroup update -g {rg} -n {sg} --identity SystemAssigned
```

**If new:** Ask for a name + location, then create:
```bash
# Create sandbox group
aca sandboxgroup create -g {rg} -n {sg} -l {location}

# Enable system-assigned managed identity (create doesn't support --identity)
aca sandboxgroup update -g {rg} -n {sg} --identity SystemAssigned
# Verify: aca sandboxgroup show -g {rg} -n {sg} --query "identity.principalId"
```

> **⚠️ New groups take 5–20 min to propagate to the data plane.** Prefer reusing existing groups when possible.

**Then create a sandbox** (in existing or new group):
```bash
# Create sandbox (retry with backoff if SandboxGroupNotFound)
aca sandbox create -g {rg} --group {sg} --disk ubuntu

# Wait for Running state
aca sandbox show -g {rg} --group {sg} --id {sandbox_id} --query "state"

# Install Python if handler uses it (ubuntu image has no Python pre-installed)
aca sandbox exec -g {rg} --group {sg} --id {sandbox_id} -c "apt update && apt install -y python3 python3-pip python3-requests"
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

> **⚠️ The trigger API uses `metadata` + `notificationDetails` with a full `callbackUrl`. `callbackTarget` does NOT exist.**
> The correct schema: `connectionDetails` (connector+connection), `metadata` (sandbox info), `notificationDetails` (callbackUrl, auth, body), `operationName`, `parameters`.

```powershell
# Build trigger config body — ShellCommand example
# First construct the callback URL:
$callbackUrl = "https://management.azuredevcompute.io/subscriptions/{sub}/resourceGroups/{rg}/sandboxGroups/{sandbox_group}/sandboxes/{sandbox_id}/executeShellCommand?api-version=2026-02-01-preview"

$triggerBody = @{
  properties = @{
    connectionDetails = @{
      connectorName = "office365"
      connectionName = "{conn}"
    }
    metadata = @{
      sandboxGroupName = "{sandbox_group}"
      sandboxId = "{sandbox_id}"
    }
    notificationDetails = @{
      authentication = @{ audience = "https://management.azuredevcompute.io/"; type = "ManagedServiceIdentity" }
      body = @{ activationMode = "OnDemand"; command = "python /app/handler.py" }
      callbackUrl = $callbackUrl
      httpMethod = "Post"
    }
    operationName = "OnNewEmailV3"
    parameters = @(
      @{ name = "folderPath"; value = "Inbox" }
    )
  }
} | ConvertTo-Json -Depth 6 -Compress

# For InvokePort target — replace notificationDetails with:
#   callbackUrl = "https://{sandbox_id}--5000.proxy.azuredevcompute.io/webhook"
#   httpMethod = "Post"  (omit body and authentication from notificationDetails)
#   NOTE: Port-level auth IS still required — add gateway principalId to port's entraId objectIds
# For ExecuteCommand target — change callbackUrl to .../executeCommand and body to:
#   @{ activationMode = "OnDemand"; command = "python"; args = @("/app/handler.py") }

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
