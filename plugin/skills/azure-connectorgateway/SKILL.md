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
| **No notebooks/scripts for setup** | Walk user through interactively. Execute `az rest` commands directly. |
| **No MCP configs** | Sandbox apps run without an agent. Call connection runtime URL directly via HTTP. Egress transform handles auth. If you reach `mcp-config create`, STOP. |
| **No guessing dynamic values** | If a parameter has `x-ms-dynamic-*`, you MUST call the API, present results, and wait for user selection. Never assume a team/channel/folder/site. |
| **Execute, don't ask** | Gather user inputs → execute operations immediately → report result. Never say "Can I run this?" |
| **Two script types** | Setup = `az rest` commands (no files, no extensions). Handler = Python file deployed to sandbox via `aca sandbox fs write` (calls runtime URL via HTTP). |
| **SSL in sandbox** | Use `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt` (preferred). Fallback: `verify=False` + `urllib3.disable_warnings()`. **stderr = trigger failure** — never leave warnings unsuppressed. |
| **Parallel execution** | Run independent operations (connections, ACLs, egress, dynamic values) as parallel tool calls. |
| **Tool permissions** | If "Permission denied", ask user to enable autopilot mode, then retry. |
| **Deploy handler** | Write Python to local file → `aca sandbox fs write` to upload. NEVER pass Python code as inline PowerShell string (f-strings/braces break). |
| **Trigger body schema** | API uses `connectionDetails` + `notificationDetails` objects, NOT flat fields. The SDK's `create_trigger()` sends `metadata` which the API rejects. Always use `az rest` with `@$tmpFile`. |
| **exec vs exec_command** | `aca sandbox exec -c "python /app/handler.py"` (shell-interpreted). Do NOT use `exec_command` with spaces in the string — it treats the whole string as a binary path. |
| **No az extensions** | Do NOT use `az connectorgateway`, `az sandbox`, or `az sandboxgroup`. These are NOT required. All gateway ops = `az rest`. All sandbox ops = `aca` CLI. |
| **Install aca CLI first** | Before any sandbox operations, check `aca --version`. If missing: `npm install -g https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases/download/v0.1.0b1/azure-containerapps-cli-1.0.0-beta.1.tgz`. Do NOT try alternative approaches — aca CLI is the only way. |

**When to STOP and ask the user:** Any parameter with dynamic values (teams, channels, folders, sites, lists), choosing integration pattern, OAuth consent. **You must NEVER skip this — always fetch the list and present it.**

**When to EXECUTE immediately:** creating gateways/connections/triggers/policies, deploying handlers, setting egress, installing deps.

### Step 0: Prerequisites (run silently)
Before starting, check and install tools. Do NOT ask — just install if missing:
```powershell
# Check az login
az account show --query "{sub:id,tenant:tenantId}" -o json
# If fails → tell user to run: az login

# Check aca CLI — REQUIRED for all sandbox operations
aca --version
# If missing → try install:
gh release download v0.1.0b1 --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure-containerapps-cli-*.tgz" --dir $env:TEMP
npm install -g (Get-ChildItem "$env:TEMP/azure-containerapps-cli-*.tgz").FullName

# If aca install fails (404) → check if sandbox SDK is available as fallback:
pip show sandbox-sdk 2>$null
python -c "from sandbox import SandboxClient; print('SDK available')" 2>$null
# If SDK found: use SandboxClient for sandbox ops (write_file, exec, etc.)
# If neither aca nor SDK available → ask user for help
```
> **⚠️ There are NO `az` commands for sandboxes.** Do NOT use `az sandbox`, `az sandboxgroup`,
> or `az connectorgateway`. Gateway = `az rest`. Sandbox = `aca` CLI (preferred) or Python SDK fallback.
> SDK import: try `from sandbox import SandboxClient` first, then `from azure.containerapps.sandbox import SandboxClient`.

### Step 1: Understand the scenario
Ask the user:
- "What event do you want to trigger on?" (new email, SharePoint list item, file upload, etc.)
- Map the answer to a connector: `office365`, `sharepointonline`, `onedriveforbusiness`, etc.
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

Generate consent URLs — POST to `listConsentLinks`, then **open each in the user's browser automatically**.

> **⚠️ The body format MUST be exactly as shown below. Do NOT try other formats.**

```powershell
# Get the connection's objectId and tenantId first
$conn = az rest --method GET `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}?api-version=2026-05-01-preview" | ConvertFrom-Json
$objectId = $conn.properties.authenticatedUser.name
$tenantId = $conn.properties.authenticatedUser.tenantId

# Build consent body — EXACT format required (parameters array)
$body = @{
  parameters = @(@{
    objectId = $objectId
    tenantId = $tenantId
    redirectUrl = "https://microsoft.com"
    parameterName = "token"
  })
} | ConvertTo-Json -Depth 3 -Compress

# Post and open in browser
$tmpFile = New-TemporaryFile
Set-Content $tmpFile $body
$link = az rest --method POST `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/listConsentLinks?api-version=2026-05-01-preview" `
  --body "@$tmpFile" --query "value[0].link" -o tsv
Remove-Item $tmpFile
Start-Process $link
```

> **⚠️ ALWAYS use `Start-Process` to open consent links in the browser.**
> Do NOT just print the URL — it's too long to copy and must be opened automatically.
> Use `"redirectUrl":"https://microsoft.com"` — default redirect is broken.
> Consent is auto-confirmed during the flow; no code pasting needed.
> **Do NOT retry with different body formats** — if consent fails, it's a service issue.

Ask user to authenticate (use `ask_user`), then verify:
```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections?api-version=2026-05-01-preview" \
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

Call connector operations via `az rest` to the ARM `dynamicInvoke` endpoint.
Gateway injects stored OAuth credentials. **Use `request` format (NOT `parameters`).**

> **⚠️ Do NOT include `Content-*` headers in the request object.**

1. **Select the operation** based on user's goal:
   ```bash
   az rest --method POST \
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/listOperations?api-version=2026-05-01-preview" \
     --body '{"connectorName":"{connector}"}'
   ```
   Match user's intent to the best operation. If ambiguous, ask with specific choices.
   Do NOT dump all operations for the user — choose the right one yourself.

2. **Collect parameter values interactively.** For each required parameter, check
   its Swagger extension. Use `@$tmpFile` for `az rest --body` with special chars.

   > **🚫 NEVER guess or infer dynamic parameter values. NEVER use placeholder IDs.**
   > If a parameter has `x-ms-dynamic-values`, `x-ms-dynamic-list`, `x-ms-dynamic-tree`,
   > or `x-ms-dynamic-schema`, you MUST:
   > 1. Call the specified operationId via `dynamicInvoke` to fetch the actual values
   > 2. Present the results to the user with `ask_user`
   > 3. STOP and wait for the user to select — do NOT proceed until they answer
   >
   > **Wrong:** "I'll use the Inbox folder" (guessed without calling the API)
   > **Wrong:** Using a teamId/channelId/siteUrl without fetching the list first
   > **Right:** Call the dynamic operation → show user the list → wait for selection

   **Parameter resolution by extension type:**

   | Extension | What it does | How to handle |
   |-----------|-------------|---------------|
   | `x-ms-dynamic-values` | Flat list of options | Call operationId → present choices → **STOP** |
   | `x-ms-dynamic-list` | Same as above (nested variant) | Same as dynamic-values → **STOP** |
   | `x-ms-dynamic-tree` | Hierarchical folder browsing | Call open → present → browse deeper → **STOP at each level** |
   | `x-ms-dynamic-schema` | Fields depend on prior selection | Collect dependencies first → call schema op → **STOP** |
   | Static enum | Fixed choices in Swagger | Present choices → **STOP** |
   | Free-form | User provides value | Ask user (or use obvious default + inform) |

   → **Full algorithms with code examples:** See [dynamic-values.md](references/dynamic-values.md)

   **Key rules:**
   - **STOP at every dynamic parameter** — even if you think you know the answer.
     The user's Teams, channels, SharePoint sites, and folders are NOT predictable.
   - **Always resolve `operationId` to HTTP path** from the Swagger — do NOT guess
   - **Always URL-encode IDs** with `[System.Uri]::EscapeDataString()`
   - **Use `@file` pattern** for `az rest --body` when IDs contain `!` or special chars
   - **Skip optional parameters** unless user mentioned them
   - **Large lists (>10 items):** Do NOT pass all items as `ask_user` choices.
     Instead, print the numbered list in your response text and use `ask_user`
     with `allow_freeform: true` (no `choices` array): "Type the name or number."
     Only use the `choices` array when there are ≤10 items.
   - **Very large lists (50+):** Fetch with `$top=20`, show results, ask
     "Do you see yours, or should I load more?"

   **Do NOT proceed to step 3 until ALL required parameters are confirmed by the user.**

3. **Build and call `dynamicInvoke`.** The request object supports:
   - `method` — HTTP method (GET, POST, PUT, DELETE)
   - `path` — operation path from Swagger (strip `/{connectionId}` prefix)
   - `queries` — query parameters as key-value dict
   - `body` — request body (string or object)
   - `headers` — HTTP headers (**except** `Content-*` headers)

   Map Swagger `in` field: `path` → URL path, `query` → queries dict,
   `body` → body field, `header` → headers dict (except `Content-*`).

   ```bash
   # Example: Create a file in OneDrive
   az rest --method POST \
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" \
     --body '{
       "request": {
         "method": "POST",
         "path": "/datasets/default/files",
         "queries": {"folderPath": "/", "name": "hello.txt"},
         "body": "Hello from Connector Gateway!"
       }
     }'
   ```

   → **More examples:** See [runtime-url-examples.md](references/runtime-url-examples.md)

4. **If running from a sandbox**, set up ACL + egress (run in parallel):
   ```bash
   # Get runtime URL host
   az rest --method GET \
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}?api-version=2026-05-01-preview" \
     --query "properties.connectionRuntimeUrl" -o tsv
   ```
   Then set egress transform. **Critical values:**
   - Token resource: `https://management.core.windows.net/` (NOT `management.azure.com`)
   - Header format: `"Bearer {value}"` (NOT `{token}`)
   - Sandbox MUST be running before setting egress
   - One rule covers all connections on same gateway host

   → **Full egress setup code + troubleshooting:** See [egress-setup.md](references/egress-setup.md)
   → **Runtime URL examples for sandbox apps:** See [runtime-url-examples.md](references/runtime-url-examples.md)

   **Two auth patterns:**
   | Context | Pattern |
   |---------|---------|
   | **Setup** (dynamic values, testing) | `dynamicInvoke` via ARM (uses Azure CLI identity) |
   | **Sandbox runtime** (deployed handler) | `connectionRuntimeUrl` + egress transform (uses sandbox MI) |

**→ Skip to Final verification checklist (Direct API).**

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

- [trigger-setup.md](references/trigger-setup.md) — Full trigger creation commands (Steps 5B–9B)
- [handler-guide.md](references/handler-guide.md) — Handler development, event delivery, templates
- [dynamic-values.md](references/dynamic-values.md) — Dynamic parameter resolution algorithms
- [egress-setup.md](references/egress-setup.md) — ACL + egress transform + troubleshooting
- [runtime-url-examples.md](references/runtime-url-examples.md) — Curl examples for all connectors
- [gotchas.md](references/gotchas.md) — Common issues and solutions
- [trigger-flow.md](references/trigger-flow.md) — Trigger architecture details
- [prerequisites.md](references/prerequisites.md) — Setup requirements
- [quickstart.md](references/quickstart.md) — Quick start guide
