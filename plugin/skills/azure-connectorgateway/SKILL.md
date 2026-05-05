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
| **No hallucination** | Use `--help` on any command. Check `references/` for details. |
| **No notebooks/scripts for setup** | Walk user through interactively. Execute `az` commands directly. |
| **No MCP configs** | Sandbox apps run without an agent. Call connection runtime URL directly via HTTP. Egress transform handles auth. If you reach `mcp-config create`, STOP. |
| **No guessing dynamic values** | If a parameter has `x-ms-dynamic-*`, you MUST call the API, present results, and wait for user selection. Never assume a team/channel/folder/site. |
| **Execute, don't ask** | Gather user inputs → execute operations immediately → report result. Never say "Can I run this?" |
| **Two script types** | Setup = `az` CLI commands (no files). Handler = Python file deployed to sandbox (calls runtime URL via HTTP). |
| **SSL in sandbox** | Use `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt` (preferred). Fallback: `verify=False` + `urllib3.disable_warnings()`. **stderr = trigger failure** — never leave warnings unsuppressed. |
| **Parallel execution** | Run independent operations (connections, ACLs, egress, dynamic values) as parallel tool calls. |
| **Tool permissions** | If "Permission denied", ask user to enable autopilot mode, then retry. |

**When to STOP and ask the user:** Any parameter with dynamic values (teams, channels, folders, sites, lists), choosing integration pattern, OAuth consent. **You must NEVER skip this — always fetch the list and present it.**

**When to EXECUTE immediately:** creating gateways/connections/triggers/policies, deploying handlers, setting egress, installing deps.

### Step 1: Understand the scenario
Ask the user:
- "What event do you want to trigger on?" (new email, SharePoint list item, file upload, etc.)
- Map the answer to a connector: `office365`, `sharepointonline`, `onedriveforbusiness`, etc.
- Ask if they already know the trigger operation, or want to discover available ones.

**Stop and wait for the user's answer before continuing.**

### Step 2: Gateway setup
Ask the user:
- "Do you have an existing connector gateway, or should I create a new one?"
- If **existing**: ask for resource group + gateway name, then retrieve it:
  ```bash
  az connectorgateway gateway show -g {rg} -n {gw} --query "{name:name, principalId:identity.principalId, tenantId:identity.tenantId}" -o json
  ```
- If **new**: ask for resource group + gateway name + location, then **create it
  immediately** with a SystemAssigned managed identity (required for trigger callbacks):
  ```bash
  az connectorgateway gateway create -g {rg} -n {gw} -l {location} --identity SystemAssigned --query "{name:name, principalId:identity.principalId, tenantId:identity.tenantId}" -o json
  ```
- **Always** capture `principalId` and `tenantId` — they are needed later for
  access policies and InvokePort auth.
- List existing connections:
  ```bash
  az connectorgateway connection list -g {rg} --gateway {gw} -o table
  ```

**Once you have the gateway info, proceed immediately to Step 3.**

### Step 3: Create connection(s) + authenticate

Create ALL needed connections in parallel, then consent all at once:

```bash
# Create connections (parallel tool calls if multiple):
az connectorgateway connection create -g {rg} --gateway {gw} -n o365-conn --api office365 -l {location} -o json
az connectorgateway connection create -g {rg} --gateway {gw} -n onedrive-conn --api onedriveforbusiness -l {location} -o json
```

Generate consent URLs with `--redirect-url "https://microsoft.com"` (avoids broken default redirect):
```bash
az connectorgateway connection consent -g {rg} --gateway {gw} -n {conn} --redirect-url "https://microsoft.com" -o tsv
# Open the URL in browser for user
```

Ask user to authenticate (use `ask_user`), then verify:
```bash
az connectorgateway connection list -g {rg} --gateway {gw} --query "[].{name:name, status:properties.statuses[0].status}" -o table
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
   az connectorgateway trigger operations list -g {rg} --gateway {gw} --connector-type {connector} -o table
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
   az connectorgateway connection show -g {rg} --gateway {gw} -n {conn} --query "properties.connectionRuntimeUrl" -o tsv
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

## Install

```bash
# az CLI extension
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "az_cli_connectorgateway-*-py3-none-any.whl" --dir /tmp
az extension add --source /tmp/az_cli_connectorgateway-*-py3-none-any.whl

# Python SDK
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_connectorgateway-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_connectorgateway-*-py3-none-any.whl
```

## Quick reference

```bash
az connectorgateway gateway show -g {rg} -n {gw} -o json
az connectorgateway connection list -g {rg} --gateway {gw} -o table
az connectorgateway trigger list -g {rg} --gateway {gw} -o table
az connectorgateway trigger operations list -g {rg} --gateway {gw} --connector-type {type}
az connectorgateway trigger create -g {rg} --gateway {gw} -n {name} --connector-name {conn_type} --connection-name {conn} --operation-name {op} --sandbox-id {id} -s {sg} --port 5000 --port-path /webhook
```

Run `az connectorgateway --help` or `help(TriggerClient)` for full API.

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
