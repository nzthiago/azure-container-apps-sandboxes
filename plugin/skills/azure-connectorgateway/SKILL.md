---
name: azure-connectorgateway
description: |
  Azure Connector Gateway — manage gateways, connections, and triggers.
  Connects external services (Office 365, GitHub, Azure Blob) to sandbox apps
  via event-driven triggers or direct API calls using connection runtime URLs.
  Use when:
  - Creating or managing connector gateways and connections
  - Creating or managing trigger configs on a connector gateway
  - Subscribing to connector events (email, file, webhook)
  - Wiring event sources to sandbox callbacks
  - Managing trigger lifecycle (enable, disable, delete)
  - Building sandbox apps that call connector APIs (send email, upload files, etc.)
  Triggers: "create trigger", "trigger config", "webhook trigger",
  "connector gateway", "connection", "email trigger", "send email",
  "onedrive", "sharepoint"
---

# Azure Connector Gateway

Manage connector gateways, connections, and triggers — connect external services
to sandbox apps via direct API calls or event-driven triggers.

## Rules (MUST follow)

| Rule | Details |
|------|---------|
| **No hallucination** | Check `references/` for details. Use `az rest --help` for syntax. |
| **No notebooks/scripts** | Walk user through interactively. Execute `az rest` commands directly. |
| **No MCP configs** | Sandbox apps call runtime URL directly via HTTP. If you reach `mcp-config create`, STOP. |
| **No guessing dynamic values** | `x-ms-dynamic-*` → call API, present results, STOP. Never assume a team/channel/folder/site. |
| **Execute, don't ask** | Gather inputs → execute immediately → report. Never say "Can I run this?" |
| **No az extensions** | Gateway = `az rest`. Sandbox = `aca` CLI. Do NOT use `az connectorgateway/sandbox/sandboxgroup`. |
| **Always `@$tmpFile`** | For `az rest --body` — inline JSON breaks in PowerShell. See [gotchas.md](references/gotchas.md). |
| **Trigger body schema** | Uses `connectionDetails` + `notificationDetails`. SDK `create_trigger()` is broken. See Step 5B template. |
| **Handler deploy** | Write to local file → `aca sandbox fs write`. Never inline Python in PowerShell. |
| **SSL/stderr** | `REQUESTS_CA_BUNDLE` preferred. `verify=False` needs `disable_warnings()`. stderr = trigger failure. See [handler-guide.md](references/handler-guide.md). |
| **Parallel execution** | Run independent ops (connections, ACLs, egress, dynamic values) as parallel tool calls. |

**When to STOP and ask the user:** Any parameter with dynamic values (teams, channels, folders, sites, lists), choosing integration pattern, OAuth consent. **You must NEVER skip this — always fetch the list and present it.**

**When to EXECUTE immediately:** creating gateways/connections/triggers/policies, deploying handlers, setting egress, installing deps.

### Step 0: Prerequisites (run silently)
Check `az account show` and `aca --version`. If missing, see [prerequisites.md](references/prerequisites.md) for install + SDK fallback.

### Step 1: Understand the scenario
Ask the user:
- "What event do you want to trigger on?" (new email, SharePoint list item, file upload, etc.)
- Map the answer to a connector using this table:

| User says | Connector name | Common triggers |
|-----------|---------------|-----------------|
| Email, Outlook | `office365` | `OnNewEmailV3`, `OnFlaggedEmail` |
| SharePoint, list | `sharepointonline` | `OnNewItem`, `OnUpdatedItem` |
| OneDrive, files | `onedriveforbusiness` | `OnNewFile`, `OnUpdatedFile` |
| Teams | `teams` | `OnNewChannelMessage` |
| Azure Blob | `azureblob` | `OnNewBlob`, `OnUpdatedBlob` |

- Ask if they already know the trigger operation, or want to discover available ones.

**Stop and wait for the user's answer before continuing.**

### Step 2: Gateway setup

> **⚡ Parallel batch:** Once you know the gateway name, run ALL of these in one parallel call:
> 1. Get gateway info (principalId, tenantId, location)
> 2. List existing connections (names, statuses, runtime URLs)
> 3. Get sandbox group identity (if sandbox already exists)
>
> This avoids sequential round-trips and saves ~2 minutes.

> **ARM base URL:** `https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways`
> **API version:** `api-version=2026-05-01-preview`
> Use `az account show --query id -o tsv` to get the subscription ID.

Ask the user:
- "Do you have an existing connector gateway, or should I create a new one?"
- If **existing**: ask for resource group + gateway name, then retrieve it:
  ```bash
  az rest --method GET \
    --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}?api-version=2026-05-01-preview" \
    --query "{name:name, principalId:identity.principalId, tenantId:identity.tenantId}"
  ```
- If **new**: ask for resource group + gateway name + location, then **create it
  immediately** with a SystemAssigned managed identity (required for trigger callbacks):
  ```bash
  az rest --method PUT \
    --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}?api-version=2026-05-01-preview" \
    --body '{"location":"{location}","identity":{"type":"SystemAssigned"}}' \
    --query "{name:name, principalId:identity.principalId, tenantId:identity.tenantId}"
  ```
- **Always** capture `principalId` and `tenantId` — they are needed later for
  access policies and InvokePort auth.
- List existing connections:
  ```bash
  az rest --method GET \
    --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections?api-version=2026-05-01-preview" \
    --query "value[].{name:name, status:properties.statuses[0].status, api:properties.api.name}"
  ```

**Once you have the gateway info, proceed immediately to Step 3.**

### Step 3: Create connection(s) + authenticate

Create ALL needed connections in parallel, then consent all at once:

```bash
# Create connections (parallel tool calls if multiple):
az rest --method PUT \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/o365-conn?api-version=2026-05-01-preview" \
  --body '{"properties":{"api":{"name":"office365"}},"location":"{location}"}'

az rest --method PUT \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/onedrive-conn?api-version=2026-05-01-preview" \
  --body '{"properties":{"api":{"name":"onedriveforbusiness"}},"location":"{location}"}'
```

Generate consent links and open in browser. → **Exact format:** See [consent.md](references/consent.md)

> **⚠️ Use `Start-Process` to open links. Body MUST use `parameters` array with
> `objectId`/`tenantId` from the connection. Do NOT try other formats or print the URL.**

Ask user to authenticate (use `ask_user`), then verify:
```bash
az rest --method GET \
  --url ".../{gw}/connections?api-version=2026-05-01-preview" \
  --query "value[].{name:name, status:properties.statuses[0].status}"
# All should show: Connected. If not, re-consent.
```

### Step 4: Choose integration pattern
Ask the user:
- **A) Direct API calls** — call connector operations on demand via `dynamicInvoke`
  (send email, read list, create file). If deploying to sandbox, also sets up egress.
- **B) Event-driven triggers** — gateway pushes notifications to sandbox when
  events happen. Handler can then use direct API calls for additional actions.

**Stop and wait for the user's answer.**

- If **A** → **Step 5A**
- If **B** → **Step 5B**

---

### Step 5A: Direct API calls via dynamicInvoke

→ **Full details:** See [direct-api.md](references/direct-api.md)

**Summary:** Call `dynamicInvoke` endpoint with `{"request": {"method":"...", "path":"...", "queries":{}, "body":...}}`.
Do NOT include `Content-*` headers. Gateway injects stored OAuth credentials.

```bash
# List operations for a connector
az rest --method POST \
  --url ".../{gw}/listOperations?api-version=2026-05-01-preview" \
  --body '{"connectorName":"{connector}"}'

# Call an operation
az rest --method POST \
  --url ".../{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" \
  --body '{"request":{"method":"POST","path":"/v2/Mail","body":{...}}}'
```

> **🚫 Dynamic values:** If a parameter has `x-ms-dynamic-*`, STOP — call the API,
> present results, wait for user selection. NEVER guess folder/site/channel IDs.

**If deploying to sandbox:** Set up ACL + egress. See [egress-setup.md](references/egress-setup.md).
- Token resource: `https://management.core.windows.net/`
- Header format: `"Bearer {value}"`

**→ Skip to Final verification checklist.**

---

### Step 5B: Event-driven triggers

→ **Full trigger setup commands (Steps 5B–9B):** See [trigger-setup.md](references/trigger-setup.md)

**Trigger body template (copy-paste ready):**
```powershell
$triggerBody = @{
  properties = @{
    connectionDetails = @{ connectorName = "{connector}"; connectionName = "{conn}" }
    notificationDetails = @{
      operationName = "{operation}"
      parameters = @( @{ name = "{param}"; value = "{value}" } )
    }
    callbackTarget = @{
      sandboxId = "{sandbox_id}"; sandboxGroupName = "{sg}"
      command = "python /app/handler.py"  # ShellCommand
      # OR: port = 5000; portPath = "/webhook"; httpMethod = "POST"  # InvokePort
    }
  }
} | ConvertTo-Json -Depth 6 -Compress
$tmp = New-TemporaryFile; Set-Content $tmp $triggerBody
az rest --method PUT `
  --url ".../{gw}/triggerConfigs/{name}?api-version=2026-05-01-preview" `
  --body "@$tmp"
Remove-Item $tmp
```
> **⚠️ Do NOT use the Python SDK `create_trigger()`** — it sends a `metadata` field the API rejects.
> Always use `az rest` with the schema above (`connectionDetails` + `notificationDetails`).

**Summary of the trigger flow:**
1. Discover trigger operations → present to user → STOP and wait
2. Collect trigger parameters (same dynamic value rules as Step 5A)
3. Ask user for sandbox (existing or new) + callback type (ShellCommand / InvokePort)
4. Create trigger + access policy + role assignment (**run in parallel**)
5. Verify trigger state is `Enabled`

**Key decisions:**
- **ShellCommand**: auto-resumes sandbox, but does NOT pass event data to handler
- **InvokePort**: passes event data in POST body, but sandbox must be running
- **ShellCommand + ExecuteCommand**: need RBAC role `c24cf47c-5077-412d-a19c-45202126392c` on sandbox group
- **InvokePort**: needs port auth (gateway principalId in entraId objectIds)

After trigger creation → proceed to handler deployment.
See [handler-guide.md](references/handler-guide.md) for handler development.

---

### Final verification checklist

**For Direct API calls (path A):**
- ✅ Gateway exists, connection `Connected`, `connectionRuntimeUrl` available
- ✅ Access policy: sandbox group MI → connection
- ✅ Egress transform: resource `https://management.core.windows.net/`, format `Bearer {value}`
- ✅ Test call from sandbox works (no auth header needed)

**For Event-driven triggers (path B):**
- ✅ Gateway has SystemAssigned identity, connection `Connected`
- ✅ Trigger state is `Enabled`, access policy exists (gateway MI → connection)
- ✅ RBAC role on sandbox group (ShellCommand) OR port auth (InvokePort)
- ✅ If handler calls runtime URL: also needs egress + ACL (same as path A)

After setup → deploy the handler app. See [handler-guide.md](references/handler-guide.md).

## Quick reference

```bash
# ARM base: https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways
# API version: api-version=2026-05-01-preview

# Gateway
az rest --method GET --url ".../connectorGateways/{gw}?api-version=2026-05-01-preview"

# Connections
az rest --method GET --url ".../connectorGateways/{gw}/connections?api-version=2026-05-01-preview"

# List operations
az rest --method POST --url ".../connectorGateways/{gw}/listOperations?api-version=2026-05-01-preview" --body '{"connectorName":"{type}"}'

# Dynamic invoke
az rest --method POST --url ".../connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" --body '{"request":{"method":"GET","path":"/..."}}'

# Trigger configs
az rest --method GET --url ".../connectorGateways/{gw}/triggerConfigs?api-version=2026-05-01-preview"
az rest --method GET --url ".../connectorGateways/{gw}/triggerConfigs/{name}?api-version=2026-05-01-preview" --query "properties.state"
```

## References

- [direct-api.md](references/direct-api.md) — Full dynamicInvoke details, parameter resolution, examples
- [consent.md](references/consent.md) — OAuth consent link generation (exact body format)
- [trigger-setup.md](references/trigger-setup.md) — Full trigger creation commands (Steps 5B–9B)
- [handler-guide.md](references/handler-guide.md) — Handler development, event delivery, templates
- [dynamic-values.md](references/dynamic-values.md) — Dynamic parameter resolution algorithms
- [egress-setup.md](references/egress-setup.md) — ACL + egress transform + troubleshooting
- [runtime-url-examples.md](references/runtime-url-examples.md) — Curl examples for all connectors
- [gotchas.md](references/gotchas.md) — Common issues and solutions
- [trigger-flow.md](references/trigger-flow.md) — Trigger architecture details
- [prerequisites.md](references/prerequisites.md) — Setup requirements
- [quickstart.md](references/quickstart.md) — Quick start guide
