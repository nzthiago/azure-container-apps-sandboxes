# Trigger Setup (Steps 5B–9B)

Detailed commands for creating event-driven triggers on a connector gateway.

## Step 5B: Discover trigger operations

```bash
az connectorgateway trigger operations list -g {rg} --gateway {gw} --connector-type office365 -o table
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
az sandbox group list -g {rg} -o table

# Create new group if needed
az sandbox group create -g {rg} -n {sg} -l {location} --identity SystemAssigned -o json

# Create sandbox (retry with backoff if SandboxGroupNotFound)
az sandbox create -g {rg} -s {sg} --disk ubuntu -o json

# Wait for Running state
az sandbox show -g {rg} -s {sg} -n {sandbox_id} --query "state" -o tsv
```

> **⚠️ Identity (principalId) is on the sandbox GROUP, not individual sandboxes.**
> If group lacks identity: `az sandbox group update -g {rg} -n {sg} --identity SystemAssigned`

Ask for callback type:
- **ShellCommand** — `python /app/handler.py` (shell-interpreted)
- **ExecuteCommand** — `python` with args (no shell)
- **InvokePort** — POST to port 5000, path `/webhook`

## Step 8B: Create trigger + access policy + role assignment

> **⚡ These three operations are independent — run in parallel.**

### Trigger creation

```powershell
# ShellCommand target:
az connectorgateway trigger create -g {rg} --gateway {gw} -n {trigger_name} `
  --connector-name office365 --connection-name {conn} `
  --operation-name OnNewEmailV3 `
  --sandbox-id {sandbox_id} -s {sandbox_group} `
  --command "python /app/handler.py" `
  --parameters '[{\"name\": \"folderPath\", \"value\": \"Inbox\"}]' -o json

# InvokePort target:
az connectorgateway trigger create -g {rg} --gateway {gw} -n {trigger_name} `
  --connector-name office365 --connection-name {conn} `
  --operation-name OnNewEmailV3 `
  --sandbox-id {sandbox_id} -s {sandbox_group} `
  --port 5000 --port-path /webhook `
  --parameters '[{\"name\": \"folderPath\", \"value\": \"Inbox\"}]' -o json
```

> **⚠️ If `--command` fails with KeyError** (CLI bug), use Python SDK:
> ```python
> from azure.connectorgateway import TriggerClient
> tc = TriggerClient(resource_group='{rg}')
> tc.create_trigger('{gw}', '{trigger_name}',
>     connector_name='office365', connection_name='{conn}',
>     operation_name='OnNewEmailV3',
>     sandbox_id='{sandbox_id}', sandbox_group='{sandbox_group}',
>     command='python /app/handler.py',
>     parameters=[{'name': 'folderPath', 'value': 'Inbox'}])
> ```

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
az sandbox port add -g {rg} -s {sandbox_group} -n {sandbox_id} --port 5000 \
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
az connectorgateway trigger show -g {rg} --gateway {gw} -n {trigger} --query "properties.state" -o tsv
# Should output: Enabled
```
