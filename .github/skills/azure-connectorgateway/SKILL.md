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

Manage connector gateways, connections, and triggers — connect
external services to sandbox apps (run command, call HTTP port, direct API calls).

> **⚠️ Do NOT hallucinate CLI commands or API calls.**
> Use `--help` on any command. Reference files in `references/` for details.

> **⚠️ This skill requires tool execution permissions.**
> If your first command gets "Permission denied", ask the user to enable autopilot/
> auto-approve mode, then retry. Do NOT fall back to showing code blocks for the
> user to run manually.

> **⚠️ Do NOT generate a Jupyter notebook or one-shot automation.**
> Walk the user through setup **interactively** — ask questions, then execute.

> **🚫 Do NOT create MCP configs.** MCP configs wire connector operations as tools
> for an AI agent — but apps deployed to sandboxes run **without an agent present**.
> Whether the app is triggered by an event OR runs on-demand (e.g., "send an email"),
> it must call the **connection runtime URL** directly using HTTP (`requests`/`urllib`).
> The egress transform on the sandbox handles authentication automatically.
> If you find yourself running `az connectorgateway mcp-config create`, STOP —
> you are on the wrong path.

> **⚠️ Two types of scripts — know the difference:**
> - **Setup operations** (gateway, connections, triggers, ACLs, egress) →
>   execute via `az` CLI directly. Do NOT create script files for setup.
> - **App/handler scripts** (deployed to sandbox, run by triggers OR on-demand) →
>   DO create a Python script file. This app calls **connection runtime URLs**
>   directly via HTTP — egress transform injects auth. No agent, no MCP.

> **⚠️ Setup execution rules:**
> - Use `az connectorgateway` / `az rest` / `az sandbox` commands for all setup
> - Use inline `python -c "..."` ONLY for: consent flow, dynamic value pagination,
>   or egress setup (needs two tokens)
> - Do NOT create `.py` or `.sh` files for setup operations
> - Execute setup commands DIRECTLY — do NOT ask permission

> **⚠️ App/handler script rules:**
> - When the user asks to "build an app" or "create a handler", create a Python
>   script (e.g., `/app/handler.py`) that gets deployed to the sandbox
> - The script should use `requests`/`httpx` + `curl` to call the connection
>   runtime URL directly — egress transform handles auth automatically
> - Keep the script focused: receive event → call runtime URL → done
> - Deploy it to the sandbox and wire it up as the trigger callback

> **⚠️ Execution policy: Execute operations DIRECTLY — do NOT ask permission.**
> - When you need **user input** (which team? which connector? which folder?) → ASK the user.
> - When you need to **execute an operation** (create gateway, create connection,
>   create trigger, deploy handler, set egress) → JUST DO IT.
> - Do NOT say "Can I run this?" or "Shall I execute?" or "Let me create X, okay?"
> - The pattern is: gather inputs → execute → report result.

> **az CLI quick reference for setup:**
> ```bash
> az connectorgateway gateway show -g {rg} -n {gw} -o json
> az connectorgateway gateway list -g {rg} -o table
> az connectorgateway connection show -g {rg} --gateway {gw} -n {conn} -o json
> az connectorgateway connection list -g {rg} --gateway {gw} -o table
> az connectorgateway trigger show -g {rg} --gateway {gw} -n {trigger} -o json
> az connectorgateway trigger list -g {rg} --gateway {gw} -o table
> az connectorgateway trigger operations list -g {rg} --gateway {gw} --connector-type {type}
> ```

## Interactive Flow (FOLLOW THIS)

When a user asks to create a trigger, set up event-driven automation, or connect
an external service to a sandbox, **guide them interactively step by step**.
Do NOT skip to generating code or notebooks.

**When to STOP and ask the user:**
- Choosing a connector, team, channel, folder, or other dynamic value
- Choosing between integration patterns (Direct API vs Triggers)
- OAuth consent (user must complete in browser)

**When to EXECUTE immediately (no permission needed):**
- Creating gateways, connections, triggers, access policies
- Deploying handlers, setting egress rules, running scripts
- Installing dependencies, resuming sandboxes
- Any operation where the user has already told you what they want

> **⚡ Parallelism — do independent operations simultaneously:**
> Many setup operations are independent and should be run in parallel to save time.
> Use parallel tool calls (multiple calls in one response) for these patterns:
>
> | Operations | Run in parallel? |
> |-----------|-----------------|
> | Creating multiple connections (e.g., O365 + OneDrive) | ✅ Yes — create all at once |
> | Opening multiple consent URLs | ✅ Yes — open all browser tabs, user auths once per tab |
> | Connection creation + starting redirect server | ✅ Yes — start server first, create connection simultaneously |
> | ACL creation + egress transform setup | ✅ Yes — independent ARM calls |
> | Trigger creation + access policy + role assignment | ✅ Yes — all independent |
> | Fetching multiple dynamic value lists | ✅ Yes — each is an independent `az rest` call |
> | Sandbox creation + connection setup | ✅ Yes — sandbox propagation takes time, do other work while waiting |
> | Sequential consent flows (one after another) | ❌ No — batch all connections first, then consent all at once |
>
> **Key principle**: If two operations don't depend on each other's output,
> run them in the same response as parallel tool calls.

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

> **⚡ If the scenario needs multiple connectors** (e.g., O365 + OneDrive + SharePoint),
> create ALL connections in parallel, then open ALL consent URLs at once.
> Do NOT create and consent one at a time.

- **Create the connection(s)** — run these in parallel if creating multiple:
  ```bash
  # These can all run simultaneously as parallel tool calls:
  az connectorgateway connection create -g {rg} --gateway {gw} -n o365-conn --api office365 -l {location} -o json
  az connectorgateway connection create -g {rg} --gateway {gw} -n onedrive-conn --api onedriveforbusiness -l {location} -o json
  az connectorgateway connection create -g {rg} --gateway {gw} -n sharepoint-conn --api sharepointonline -l {location} -o json
  ```
- **Generate consent link and open in browser:**

  Use `https://microsoft.com` as the redirect URL — the user sees the Microsoft
  homepage after auth (instead of a broken consent error page). No local server needed.

  ```powershell
  # Generate and open consent URLs — do ALL connections in one batch
  $connections = @("o365-conn", "onedrive-conn")  # all connections that need consent
  foreach ($conn in $connections) {
      $url = az connectorgateway connection consent -g {rg} --gateway {gw} -n $conn --redirect-url "https://microsoft.com" -o tsv
      Start-Process $url
  }
  ```

  > **⚠️ The `--redirect-url` controls where the browser goes after auth.**
  > - Default redirect (`global.consent.azure-apim.net/redirect`) causes errors.
  > - Using `https://microsoft.com` as redirect shows a clean landing page.
  > - The consent is auto-confirmed during the `/confirm` step — no code pasting needed.

- **Ask user to complete authentication:**
  ```
  Use `ask_user` with:
    question: "I've opened browser windows for authentication (one per connection). Please sign in and authorize each one. You should see a green 'Authentication Successful' page for each. Let me know when you're finished with all of them."
    choices: ["Done, I've authenticated all connections"]
  ```

- **Verify ALL connection statuses** (run in parallel) **and stop the redirect server:**
  ```bash
  # Verify all connections at once
  az connectorgateway connection list -g {rg} --gateway {gw} --query "[].{name:name, status:properties.statuses[0].status}" -o table
  # All should show: Connected
  ```
  Stop the local redirect server after verification.

  If status is not `Connected`, re-generate consent link and retry.

### Step 4: Choose integration pattern
Ask the user:
- "How do you want to use this connection?"
  - **A) Direct API calls** — call connector operations on demand
    via `dynamicInvoke` (e.g., send email, read SharePoint list, create OneDrive file).
    Uses `az rest` for setup. If deploying an app to a sandbox, also sets up egress + ACL.
  - **B) Event-driven triggers** — the gateway pushes notifications to your
    sandbox when events happen (e.g., new email arrives, list item created).
    The handler app in the sandbox can then use **direct API calls** to fetch
    additional data or take actions (e.g., read email body, create files).

> **⚠️ Do NOT create MCP configs** — sandbox apps run without an agent, so MCP
> tools are useless. Both paths A and B use connection runtime URLs directly.

**Stop and wait for the user's answer before continuing.**

- If **A (Direct API calls)** → go to **Step 5A**
- If **B (Event-driven triggers)** → go to **Step 5B**

---

### Step 5A: Direct API calls via dynamicInvoke

Call connector operations directly through `az rest` to the ARM `dynamicInvoke` endpoint.
The gateway injects the stored OAuth credentials and forwards to the connector.

> **⚠️ Use `az rest` for `dynamicInvoke` during setup. Use connection runtime URLs in sandbox apps.**
> During setup (this step), use `az rest` to call `dynamicInvoke` for discovering operations,
> fetching dynamic values, and testing calls. For the actual app deployed to the sandbox,
> call the connection runtime URL directly with `requests`/`urllib` — egress transform handles auth.
> Do NOT use MCP configs — there is no agent in the sandbox to consume MCP tools.

> **⚠️ IMPORTANT: Use the `request` format, NOT the `parameters` format.**
> The `dynamicInvoke` API only accepts `{"request": {"method": ..., "path": ...}}`.
> The `{"parameters": {"operationId": ...}}` format is NOT supported and returns 400.

> **⚠️ Do NOT include `Content-*` headers** in the request object — the API rejects them.

1. **Automatically select the operation** based on the user's stated goal:
   ```bash
   az connectorgateway trigger operations list -g {rg} --gateway {gw} --connector-type {connector} -o table
   ```
   Or for the full Swagger details (needed for parameter definitions):
   ```bash
   az rest --method POST \
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/listOperations?api-version=2026-05-01-preview" \
     --body '{"connectorName": "{connector}"}'
   ```
   Match the user's intent to the best operation (e.g., "send a message to Teams"
   → `PostMessageToConversation` or `PostMessage`; "create a file" → `CreateFile`).
   Do NOT list all operations for the user to pick from — choose the right one yourself.

   If ambiguous (multiple operations could match), ask the user to clarify with
   specific choices describing the difference (e.g., "Post to a channel" vs "Post in a chat").

2. **Collect parameter values interactively.** Inspect the selected operation's
   parameters from the Swagger definition returned by `listOperations` above.
   Identify which parameters are required and what input type they expect
   (dynamic list, enum, free-form).

   For each **required** parameter, check its Swagger extension to determine how
   to collect the value. All dynamic types use the same `dynamicInvoke` endpoint —
   the difference is how parameters are resolved and results are extracted.

   > **⚠️ PowerShell JSON quoting for `az rest --body`:**
   > For simple static JSON, use escaped double quotes inside single quotes:
   > ```powershell
   > az rest --method POST --url "..." --body '{\"request\":{\"method\":\"GET\",\"path\":\"/datasets/default/folders\"}}' --headers "Content-Type=application/json"
   > ```
   > For **dynamic values** in the JSON (e.g., folder IDs with special characters),
   > use the `@file` pattern — write JSON to a temp file and pass `--body @$tmpFile`:
   > ```powershell
   > $bodyJson = '{"request":{"method":"GET","path":"/datasets/default/folders/' + $encodedId + '"}}'
   > $tmpBody = [System.IO.Path]::GetTempFileName()
   > $bodyJson | Out-File -FilePath $tmpBody -Encoding ascii -NoNewline
   > az rest --method POST --url "..." --body "@$tmpBody" --headers "Content-Type=application/json"
   > Remove-Item $tmpBody -ErrorAction SilentlyContinue
   > ```
   > The `@file` pattern is **required** when IDs contain `!`, `%`, or other
   > characters that PowerShell or `az rest` mangle. Always URL-encode IDs with
   > `[System.Uri]::EscapeDataString($id)` before embedding in the path.
   >
   > **Always include `--headers "Content-Type=application/json"`** with `az rest` for `dynamicInvoke`.

   #### `x-ms-dynamic-values` — Flat list of options
   The Swagger extension specifies an `operationId` to call and how to extract items:
   ```json
   "x-ms-dynamic-values": {
     "operationId": "GetFolders",
     "value-path": "Id",
     "value-title": "DisplayName",
     "value-collection": "value",
     "parameters": { "dataset": { "parameter": "dataset" } }
   }
   ```
   **How to handle:**
   1. Resolve `operationId` → find the operation's HTTP method + path in the Swagger
   2. Resolve `parameters` — substitute values from previously collected params:
      - `{"parameter": "dataset"}` → use the value the user already selected for `dataset`
      - `{"value": "default"}` → use literal `"default"`
      - Plain string → use as-is
   3. Call `dynamicInvoke`:
      ```powershell
      az rest --method POST `
        --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" `
        --body '{\"request\":{\"method\":\"GET\",\"path\":\"{resolved_path}\"}}' `
        --headers "Content-Type=application/json" -o json
      ```
   4. Unwrap response: extract `response.body`
   5. If `value-collection` is set (e.g., `"value"`), navigate to that array: `response.body.value`
   6. For each item: extract `value-path` (e.g., `Id`) as the value, `value-title` (e.g., `DisplayName`) as the label
   7. Present ALL items as choices via `ask_user`
   8. **STOP and wait for user selection**

   **Common examples:**
   | Connector | Parameter | Path | value-path | value-title |
   |-----------|-----------|------|-----------|-------------|
   | OneDrive | `folderPath` | `/datasets/default/folders` | `Path` | `DisplayName` |
   | Teams | `groupId` | `/beta/me/joinedTeams` | `id` | `displayName` |
   | SharePoint | `dataset` | `/datasets` | `Url` | `Name` |
   | Office365 | `folderPath` | `/datasets/default/folders` | `Path` | `DisplayName` |

   #### `x-ms-dynamic-list` — Same as dynamic-values with nesting
   Identical to `x-ms-dynamic-values` except:
   - The `operationId` may be nested: check `dynamicState.operationId` or
     `dynamicState.extension.operationId` if direct `operationId` is missing
   - Supports both `value-path` and `valuePath` (camelCase variant)
   - Handle exactly the same way as `x-ms-dynamic-values`

   #### `x-ms-dynamic-tree` — Hierarchical browsing (folder tree)
   The Swagger extension defines `open` (root) and `browse` (children) operations:
   ```json
   "x-ms-dynamic-tree": {
     "open": {
       "operationId": "ListRootFolders",
       "itemValuePath": "Id",
       "itemTitlePath": "DisplayName",
       "itemsPath": "value",
       "itemIsParent": "IsFolder eq true"
     },
     "browse": {
       "operationId": "ListChildFolders",
       "itemValuePath": "Id",
       "itemTitlePath": "DisplayName",
       "itemsPath": "value",
       "itemIsParent": "IsFolder eq true",
       "parameters": { "id": { "selectedItemValuePath": "Id" } }
     },
     "settings": { "canSelectParentNodes": true, "canSelectLeafNodes": false }
   }
   ```

   **How to handle — step-by-step algorithm:**

   **Step T1: Resolve the `open` operationId to an HTTP path.**
   Find the operation in the Swagger (from `listOperations`) whose `operationId`
   matches `open.operationId`. Extract its `method` and `path`.
   ```powershell
   # Example: if open.operationId = "ListRootFolders" resolves to GET /datasets/default/folders
   ```

   **Step T2: Call the `open` operation to get root items.**
   ```powershell
   # For static paths (no dynamic IDs), use escaped quotes:
   az rest --method POST `
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" `
     --body '{\"request\":{\"method\":\"GET\",\"path\":\"/datasets/default/folders\"}}' `
     --headers "Content-Type=application/json" `
     --query "response.body[].{Name:DisplayName, Id:Id, IsFolder:IsFolder}" -o table
   ```
   The response returns an array of items. Each item has the fields referenced by
   `itemValuePath` (e.g., `Id`), `itemTitlePath` (e.g., `DisplayName`), and
   `itemIsParent` (e.g., `IsFolder`).

   **Step T3: Present root items as choices. STOP and wait.**
   Show all items to the user. Mark folders with 📁 prefix. Include a
   "✅ Select this level (root)" option if `canSelectParentNodes` is true.
   ```
   📁 Apps
   📁 Documents
   📁 Desktop
   📁 EmailAttachments
   ✅ Select root (/)
   ```
   **STOP and wait for user selection.**

   **Step T4: If user selects a folder and wants to go deeper — BROWSE.**
   Resolve the `browse` operationId to an HTTP path. The `browse.parameters`
   tell you how to substitute the selected item's value into the path:
   - `"id": { "selectedItemValuePath": "Id" }` means: take the `Id` field from
     the selected item and substitute it for the `{id}` path parameter.
   - **URL-encode the ID** — OneDrive IDs contain `!` and other special characters.

   ```powershell
   # The selected item's Id (e.g., from Documents folder):
   $selectedId = "b!oBRIc...01EBKFNYMT34SLMMPFYFEKV2L46DV54RIE"
   $encodedId = [System.Uri]::EscapeDataString($selectedId)

   # Build the browse path by substituting the ID into the browse operation's path
   # If browse resolves to: GET /datasets/default/folders/{id}
   # Then the actual path is: /datasets/default/folders/{encodedId}
   $bodyJson = '{"request":{"method":"GET","path":"/datasets/default/folders/' + $encodedId + '"}}'
   $tmpBody = [System.IO.Path]::GetTempFileName()
   $bodyJson | Out-File -FilePath $tmpBody -Encoding ascii -NoNewline

   az rest --method POST `
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" `
     --body "@$tmpBody" `
     --headers "Content-Type=application/json" `
     --query "response.body[].{Name:DisplayName, Id:Id, Path:Path, IsFolder:IsFolder}" -o table

   Remove-Item $tmpBody -ErrorAction SilentlyContinue
   ```

   > **⚠️ MUST use `@file` pattern for browse calls.** Folder/item IDs often
   > contain `!`, `.`, and long base64 strings that break PowerShell inline quoting.
   > Always write JSON to a temp file and pass `--body @$tmpFile`.

   **Step T5: Present children + selection option. STOP and wait.**
   ```
   📁 BackupOldDesktop
   📁 Copilot
   📁 Custom Office Templates
   ✅ Select this folder (/Documents)
   ```
   **STOP and wait for user selection.**

   **Step T6: Repeat T4-T5** until the user selects a folder (clicks "✅ Select
   this folder") or selects a leaf item. Use the final item's `Path` or `Id`
   as the parameter value for the original operation.

   **Summary of the tree walk pattern:**
   ```
   open (root) → present choices → STOP
     └─ user picks "Documents" → browse(Documents.Id) → present choices → STOP
          └─ user picks "Copilot" → browse(Copilot.Id) → present choices → STOP
               └─ user picks "✅ Select this folder" → use "/Documents/Copilot"
   ```

   **Key rules for dynamic tree:**
   - **Always resolve `operationId` to path** from the Swagger — do NOT guess paths
   - **Always URL-encode IDs** with `[System.Uri]::EscapeDataString()`
   - **Always use `@file` pattern** for browse calls (IDs have special chars)
   - **Always STOP at each level** — let the user choose to go deeper or select
   - If `browse` is not defined, reuse `open` with the selected item's ID as parameter
   - The final value to use is typically the `Path` field (e.g., `/Documents/Copilot`)
     or the `Id` field, depending on what the original operation parameter expects

   #### `x-ms-dynamic-schema` — Schema depends on prior selection
   The parameter's available fields/columns change based on another parameter's value.
   The Swagger extension on the body parameter looks like:
   ```json
   "x-ms-dynamic-schema": {
     "operationId": "GetTable",
     "parameters": {
       "dataset": { "parameter": "dataset" },
       "table": { "parameter": "table" }
     },
     "value-path": "Schema/Items"
   }
   ```
   This means: "Call `GetTable` with the user's selected `dataset` and `table` values,
   then navigate the response to `Schema` → `Items` to find the available fields."

   **How to handle — step-by-step algorithm:**

   **Step S1: Identify the dependency chain.**
   The `parameters` object tells you which other parameters must be collected FIRST.
   Each entry like `"dataset": { "parameter": "dataset" }` means the schema operation
   needs the value the user already selected for `dataset`.

   These dependent parameters are usually `x-ms-dynamic-values` themselves — collect
   them in order. Example dependency chain for SharePoint `PostItem`:
   ```
   dataset (site)  ← x-ms-dynamic-values via GetDataSets
       ↓
   table (list)    ← x-ms-dynamic-values via GetTables (depends on dataset)
       ↓
   item (body)     ← x-ms-dynamic-schema via GetTable (depends on dataset + table)
   ```

   **Step S2: Collect all dependent parameters first.**
   Follow the `x-ms-dynamic-values` flow for each dependency:
   ```powershell
   # Step S2a: Get SharePoint sites (dataset parameter)
   az rest --method POST `
     --url ".../connections/{sp_conn}/dynamicInvoke?api-version=2026-05-01-preview" `
     --body '{\"request\":{\"method\":\"GET\",\"path\":\"/datasets\"}}' `
     --headers "Content-Type=application/json" `
     --query "response.body.value[].{Name:Name, Display:DisplayName}" -o table
   # → Present sites as choices. STOP and wait. User picks e.g. "https://contoso.sharepoint.com/sites/HR"

   # Step S2b: Get lists for the selected site (table parameter)
   # URL-encode the site URL since it goes in the path
   $siteEncoded = [System.Uri]::EscapeDataString("https://contoso.sharepoint.com/sites/HR")
   $bodyJson = '{"request":{"method":"GET","path":"/datasets/' + $siteEncoded + '/tables"}}'
   $tmpBody = [System.IO.Path]::GetTempFileName()
   $bodyJson | Out-File -FilePath $tmpBody -Encoding ascii -NoNewline

   az rest --method POST `
     --url ".../connections/{sp_conn}/dynamicInvoke?api-version=2026-05-01-preview" `
     --body "@$tmpBody" `
     --headers "Content-Type=application/json" `
     --query "response.body.value[].{Name:Name, Display:DisplayName}" -o table

   Remove-Item $tmpBody -ErrorAction SilentlyContinue
   # → Present lists as choices. STOP and wait. User picks e.g. "New Hires"
   ```

   **Step S3: Resolve the schema operation's path.**
   Find the operation in the Swagger whose `operationId` matches the schema's
   `operationId`. Extract its HTTP method and path. Substitute the collected
   parameter values into the path.
   ```
   GetTable → GET /$metadata.json/datasets/{dataset}/tables/{table}
   Substituted → GET /$metadata.json/datasets/{siteEncoded}/tables/{listEncoded}
   ```

   **Step S4: Call the schema operation via `dynamicInvoke`.**
   ```powershell
   $siteEncoded = [System.Uri]::EscapeDataString($selectedSite)
   $listEncoded = [System.Uri]::EscapeDataString($selectedList)
   $bodyJson = '{"request":{"method":"GET","path":"/$metadata.json/datasets/' + $siteEncoded + '/tables/' + $listEncoded + '"}}'
   $tmpBody = [System.IO.Path]::GetTempFileName()
   $bodyJson | Out-File -FilePath $tmpBody -Encoding ascii -NoNewline

   az rest --method POST `
     --url ".../connections/{sp_conn}/dynamicInvoke?api-version=2026-05-01-preview" `
     --body "@$tmpBody" `
     --headers "Content-Type=application/json" `
     --query "response.body" -o json

   Remove-Item $tmpBody -ErrorAction SilentlyContinue
   ```

   **Step S5: Navigate the response using `value-path`.**
   The `value-path` is slash-separated (e.g., `"Schema/Items"`). Walk each segment:
   ```
   response.body → navigate to "Schema" → navigate to "Items"
   ```
   The result is a JSON Schema object with a `properties` field. Each property
   is a column/field the user can populate.

   Example response after navigation to `Schema/Items`:
   ```json
   {
     "type": "object",
     "properties": {
       "Title": { "type": "string", "x-ms-display": "Title" },
       "StartDate": { "type": "string", "format": "date", "x-ms-display": "Start Date" },
       "Manager": { "type": "string", "x-ms-display": "Manager" },
       "Role": { "type": "string", "x-ms-display": "Role" }
     },
     "required": ["Title"]
   }
   ```
   Use `--query "response.body.Schema.Items.properties"` to extract directly.

   **Step S6: Present the available fields to the user. STOP and wait.**
   Show the field names, types, and which are required:
   ```
   Available columns in "New Hires" list:
   • Title (string) — REQUIRED
   • StartDate (date)
   • Manager (string)
   • Role (string)
   Which fields do you want to populate, and what values?
   ```
   **STOP and wait for the user to provide field names and values.**

   **Step S7: Build the body for the original operation.**
   Use the field names and values the user provided:
   ```json
   {
     "Title": "Jane Smith",
     "StartDate": "2026-06-01",
     "Manager": "john@contoso.com",
     "Role": "Software Engineer"
   }
   ```

   **Summary of the schema resolution pattern:**
   ```
   GetDataSets → user picks site → STOP
     └─ GetTables(site) → user picks list → STOP
          └─ GetTable(site, list) → navigate value-path → extract properties
               └─ present fields to user → STOP → user provides values
                    └─ build body JSON → call PostItem
   ```

   **Key rules for dynamic schema:**
   - **Collect dependencies in order** — the schema operation needs values from
     prior `x-ms-dynamic-values` selections (site → list → schema)
   - **Always resolve `operationId` to path** from the Swagger — do NOT guess
   - **Navigate `value-path`** by splitting on `/` and walking each key in the response
   - **Present ALL fields** with types and required markers — let the user choose
   - **Do NOT assume field values** — always ask the user what to populate
   - **URL-encode** all path parameters (site URLs, list names may contain spaces)

   #### No extension — static enum or free-form
   - If the parameter has a **static enum** in the Swagger schema, present those
     values as choices. **STOP and wait for the user's selection.**
   - If the parameter is **free-form** (string, number, etc.), ask the user to
     provide the value directly. **STOP and wait.**

   > **⚠️ Response unwrapping:**
   > The `dynamicInvoke` response has a double-wrapped format:
   > ```json
   > {"response": {"statusCode": "OK", "body": { ...actual data... }, "headers": {...}}}
   > ```
   > Always extract from `response.body`. Use `--query "response.body"` with `az rest`.

   > **⚠️ When to STOP vs. use defaults:**
   > | Parameter type | Action |
   > |---------------|--------|
   > | Any `x-ms-dynamic-*` extension | **Always STOP** — fetch, present choices, wait for user |
   > | Static enum from Swagger | **Always STOP** — present choices, wait for user |
   > | Free-form with obvious default (e.g., `folderPath=Inbox`) | **Use the default BUT tell the user**: "Using `Inbox` for folder path — let me know if you want a different folder" |
   > | Free-form with no obvious default | **Always STOP** — ask the user |
   > | Optional parameters | **Skip** unless the user mentioned them. Do NOT ask about every optional param |
   >
   > **Key rule**: If the value comes from a dynamic API call or enum, ALWAYS let
   > the user choose. If it's a well-known default (Inbox, /, root), you may use it
   > but MUST inform the user what you chose so they can correct it.

   **Stop and wait for the user's parameter values before continuing.**

3. **Build the `dynamicInvoke` payload.** The request object supports:
   - `method` — HTTP method (GET, POST, PUT, DELETE)
   - `path` — operation path from Swagger (strip the `/{connectionId}` prefix)
   - `queries` — query parameters as key-value dict
   - `body` — request body (string or object, depending on operation)
   - `headers` — HTTP headers (**except** `Content-*` headers which are rejected)

   Map Swagger parameter locations:
   - `in: path` → substitute into the `path` string
   - `in: query` → add to `queries` dict
   - `in: body` → set as `body`
   - `in: header` → add to `headers` (**except** `Content-*`)

3. **Call `dynamicInvoke` via `az rest`:**

   ```bash
   # Example: List root folders (GET, no body)
   az rest --method POST \
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" \
     --body '{"request": {"method": "GET", "path": "/datasets/default/rootfolders"}}' \
     --query "response.body" -o json
   ```

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

   ```bash
   # Example: Send email via Office 365
   az rest --method POST \
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" \
     --body '{
       "request": {
         "method": "POST",
         "path": "/v2/Mail",
         "body": {
           "To": "user@contoso.com",
           "Subject": "Welcome!",
           "Body": "<p>Hello!</p>"
         }
       }
     }'
   ```

   The response wraps the connector's response:
   ```json
   {"response": {"statusCode": "OK", "body": {...}, "headers": {...}}}
   ```

4. If running from a **sandbox**, you must set up:
   - **Access policy** (ACL) granting the sandbox group MI access to the connection
   - **Egress transform rule** that injects a Bearer token on outbound calls to the runtime URL

   > **⚡ ACL creation and egress setup are independent — run them in parallel.**
   > Also, if setting up multiple connections, create all ACLs in one batch.

   **Step 4a: Create access policy for sandbox MI on the connection:**
   ```powershell
   $body = @{
     location = "{gateway_location}"
     properties = @{
       principal = @{
         type = "ActiveDirectory"
         identity = @{ objectId = "{sandbox_group_principal_id}"; tenantId = "{tenant_id}" }
       }
     }
   } | ConvertTo-Json -Depth 5 -Compress

   az rest --method PUT `
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/accessPolicies/sandbox-acl?api-version=2026-05-01-preview" `
     --body $body
   ```

   **Step 4b: Get the connection runtime URL:**
   ```bash
   az connectorgateway connection show -g {rg} --gateway {gw} -n {conn} --query "properties.connectionRuntimeUrl" -o tsv
   ```

   **Step 4c: Set egress transform rule on the sandbox:**

   The egress transform injects an `Authorization: Bearer {token}` header using
   the sandbox's system-assigned managed identity. The sandbox code makes plain
   HTTP calls to the runtime URL with NO auth header — the platform handles it.

   > **⚠️ The sandbox MUST be running** before setting egress policy.
   > If stopped, resume it first:
   > ```python
   > python -c "
   > from azure.sandbox import SandboxClient
   > client = SandboxClient(resource_group='{rg}')
   > client.resume_sandbox('{sandbox_id}', '{sandbox_group}')
   > import time; time.sleep(5)
   > sbx = client.get_sandbox('{sandbox_id}', '{sandbox_group}')
   > print('State:', sbx.get('state'))
   > "
   > ```

   **Use the Sandbox SDK** — this is the simplest and most reliable approach:
   ```python
   python -c "
   from urllib.parse import urlparse
   from azure.sandbox import SandboxClient
   client = SandboxClient(resource_group='{rg}')
   runtime_url = '{connectionRuntimeUrl}'
   host = urlparse(runtime_url).hostname
   result = client.add_egress_transform_rule(
       '{sandbox_id}', '{sandbox_group}',
       host=host,
       headers=[{
           'operation': 'Set',
           'name': 'Authorization',
           'valueRef': {'managedIdentityRef': {
               'resource': 'https://management.core.windows.net/',
               'format': 'Bearer {value}',
               'type': 'SystemAssigned'
           }}
       }],
       name='connection-auth')
   print('Egress transform set:', result.get('rules', [{}])[-1].get('name'))
   "
   ```

   Or set the full policy (replaces all rules):
   ```python
   python -c "
   from urllib.parse import urlparse
   from azure.sandbox import SandboxClient
   client = SandboxClient(resource_group='{rg}')
   runtime_url = '{connectionRuntimeUrl}'
   host = urlparse(runtime_url).hostname
   policy = {'defaultAction': 'Allow', 'rules': [{'name': 'connection-auth', 'match': {'host': host}, 'action': {'type': 'Transform', 'headers': [{'operation': 'Set', 'name': 'Authorization', 'valueRef': {'managedIdentityRef': {'resource': 'https://management.core.windows.net/', 'format': 'Bearer {value}', 'type': 'SystemAssigned'}}}]}}]}
   result = client.set_egress_policy('{sandbox_id}', '{sandbox_group}', policy)
   print('Egress policy set. Rules:', len(result.get('rules', [])))
   "
   ```

   > **⚠️ CRITICAL details for egress transform:**
   > - **SDK methods**: `client.add_egress_transform_rule()` (appends) or `client.set_egress_policy()` (replaces all)
   > - **API endpoint**: POST to `https://management.azuredevcompute.io/.../sandboxes/{id}/egresspolicy`
   >   (lowercase `egresspolicy`, **POST** method — NOT PUT, NOT camelCase)
   > - **Token resource in the rule**: `https://management.core.windows.net/` — this is what the
   >   connection runtime URL expects for Bearer auth. NOT `https://management.azure.com/`
   >   and NOT `https://apihub.azure.com/.default`.
   > - **Format**: `"Bearer {value}"` — the `{value}` placeholder is replaced with the
   >   actual token by the platform. Do NOT use `{token}` — only `{value}` works.
   > - **Match host**: Extract the hostname from `connectionRuntimeUrl`. All connections
   >   on the same gateway share the same host (different path suffixes per connection).
   >   This means ONE egress rule covers all connections on that gateway.
   > - **type**: Must be `"SystemAssigned"` — the sandbox group's system MI.
   > - `set_egress_policy` replaces ALL rules. If the sandbox already has rules, use
   >   `add_egress_transform_rule` to append, or include existing rules in the full policy.

   **Step 4d: Test the connection from inside the sandbox:**

   After setting up ACL + egress, verify it works by making a **read-only test call**
   appropriate for the connector. Use `executeShellCommand` on the sandbox:

   ```bash
   # No auth header needed — egress transform injects it automatically
   # Use -k flag if sandbox doesn't have CA certs installed
   curl -sk "${RUNTIME_URL}/{test_path}"
   ```

   **Choose the right test call for each connector:**

   | Connector | Test call (GET, read-only) | Expected result |
   |-----------|---------------------------|-----------------|
   | **teams** | `GET {runtimeUrl}/beta/me/joinedTeams` | JSON array of teams |
   | **office365** | `GET {runtimeUrl}/v2/Mail?folderPath=Inbox&top=1` | Latest email |
   | **onedriveforbusiness** | `GET {runtimeUrl}/datasets/default/folders` | Root folder list |
   | **sharepointonline** | `GET {runtimeUrl}/datasets` | List of SharePoint sites |
   | **azureblob** | `GET {runtimeUrl}/datasets/default/foldersV2?path=/` | Container/folder list |

   Example test from sandbox:
   ```bash
   # Test Teams connection
   curl -sk "https://fec84ebb...azure-apihub.net/apim/teams/2cb5ba.../beta/me/joinedTeams" | jq '.value | length'
   # Should return a number (e.g., 32)

   # Test OneDrive connection
   curl -sk "https://fec84ebb...azure-apihub.net/apim/onedriveforbusiness/0392cd.../datasets/default/folders" | jq '.[0:3] | .[].Path'
   # Should return folder paths

   # Test Office 365 connection
   curl -sk "https://fec84ebb...azure-apihub.net/apim/office365/5458701.../v2/Mail?folderPath=Inbox&top=1" | jq '.value[0].Subject'
   # Should return email subject

   # Test SharePoint connection
   curl -sk "https://fec84ebb...azure-apihub.net/apim/sharepointonline/97d3c2.../datasets" | jq '.value[0].Name'
   # Should return site name
   ```

   If the test returns data → egress + ACL are working correctly.
   If you get `403` → ACL is missing or hasn't propagated (wait 30s, retry).
   If you get `401` or "AuthorizationToken field is required" → egress rule is wrong
   (check resource URL is `https://management.core.windows.net/`).

   The runtime URL path matches the connector's Swagger operation paths:
   - `{connectionRuntimeUrl}/{operation_path}?{query_params}`
   - Example: `{runtimeUrl}/beta/teams/conversation/message/poster/user/location/Channel`
   - Example: `{runtimeUrl}/datasets/default/files?folderPath=/&name=hello.txt`

   **Step 4e: Building requests for the connection runtime URL:**

   The request format is standard HTTP. Build the URL and body from the connector's
   Swagger operation definition:

   ```
   {HTTP_METHOD} {connectionRuntimeUrl}/{operation_path}?{query_params}
   Content-Type: application/json
   (Authorization is injected by egress — do NOT set it yourself)
   ```

   **How to map Swagger operations to runtime URL calls:**

   1. **Find the operation** from the connector's Swagger (use `az rest` to call
      `listOperations` on the gateway, or check the connector docs).

   2. **Build the URL**:
      - Base: `connectionRuntimeUrl` (from connection properties)
      - Path: the operation's `path` field from Swagger (e.g., `/v2/Mail`, `/datasets/default/files`)
      - Query params: append as `?key=value&key2=value2`

   3. **Map parameters by location** (from Swagger `in` field):
      | Swagger `in` | Where it goes |
      |--------------|---------------|
      | `path` | Substitute into URL path (e.g., `/teams/{teamId}/channels` → `/teams/abc123/channels`) |
      | `query` | Append as query string: `?folderPath=/&name=test.txt` |
      | `body` | Send as JSON request body |
      | `header` | Add as HTTP header (but NOT `Authorization` — egress handles that) |

   4. **Common connector request examples:**

      **Teams — Post message to channel:**
      ```bash
      curl -sk -X POST "${RUNTIME_URL}/beta/teams/conversation/message/poster/user/location/Channel" \
        -H "Content-Type: application/json" \
        -d '{
          "recipient": {
            "groupId": "{team_id}",
            "channelId": "{channel_id}"
          },
          "messageBody": "<p>Hello from sandbox!</p>"
        }'
      ```

      **OneDrive — Create file:**
      ```bash
      curl -sk -X POST "${RUNTIME_URL}/datasets/default/files?folderPath=%2FMyFolder&name=report.txt" \
        -H "Content-Type: application/json" \
        -d '"File content goes here as a JSON string"'
      ```

      **OneDrive — Create file with binary content:**
      ```bash
      curl -sk -X POST "${RUNTIME_URL}/datasets/default/files?folderPath=%2FMyFolder&name=image.png" \
        -H "Content-Type: application/octet-stream" \
        --data-binary @/path/to/local/file.png
      ```

      **Office 365 — Send email:**
      ```bash
      curl -sk -X POST "${RUNTIME_URL}/v2/Mail" \
        -H "Content-Type: application/json" \
        -d '{
          "emailMessage": {
            "To": "user@contoso.com",
            "Subject": "Hello from sandbox",
            "Body": "<p>This was sent via the connection runtime URL</p>"
          }
        }'
      ```

      **Office 365 — Get emails with attachments:**
      ```bash
      curl -sk "${RUNTIME_URL}/v2/Mail?folderPath=Inbox&top=5&includeAttachments=true"
      ```

      **Office 365 — Get single attachment content (by message ID + attachment ID):**
      ```bash
      curl -sk "${RUNTIME_URL}/codeless/v1.0/me/messages/{messageId}/attachments/{attachmentId}"
      # Returns JSON with contentBytes (base64-encoded)
      ```

      **SharePoint — Get list items:**
      ```bash
      curl -sk "${RUNTIME_URL}/datasets/{encoded_site_url}/tables/{list_name}/items"
      ```

      **SharePoint — Create list item:**
      ```bash
      curl -sk -X POST "${RUNTIME_URL}/datasets/{encoded_site_url}/tables/{list_name}/items" \
        -H "Content-Type: application/json" \
        -d '{"Title": "New item", "Status": "Active"}'
      ```

   > **⚠️ Important notes on request building:**
   > - URL-encode path segments and query values (spaces → `%20` or `+`)
   > - For OneDrive file content, use `Content-Type: application/octet-stream` for binary
   >   or `application/json` with a JSON string for text content
   > - The response format varies by connector — some return the created resource,
   >   some return `{"statusCode": 200}`, some return raw data
   > - For Teams `messageBody`: HTML is supported (`<p>`, `<b>`, `<a>`, etc.)
   > - For attachment content: the `contentBytes` field is base64-encoded —
   >   decode with `echo "$content" | base64 -d > file`

   > **⚠️ Two auth patterns — when to use each:**
   > | Context | Pattern | Why |
   > |---------|---------|-----|
   > | **Local setup** (interactive, fetching dynamic values) | `dynamicInvoke` via ARM | Uses your Azure CLI identity (connection owner) |
   > | **Sandbox runtime** (deployed handler, automated execution) | `connectionRuntimeUrl` + egress transform | Uses sandbox MI; `dynamicInvoke` fails with `AIGatewayConnectionOwnerAccessDenied` from sandbox MI |

**→ Skip to Final verification checklist (Direct API).**

---

### Step 5B: Discover trigger operations
- List available trigger operations for the connector:
  ```bash
  az connectorgateway trigger operations list -g {rg} --gateway {gw} --connector-type office365 -o table
  ```
- Present the operations to the user as choices (show summary + operationId).
- Let the user pick which trigger operation to use.

**Stop and wait for the user's selection before continuing.**

### Step 6B: Collect trigger parameters
- Based on the selected operation, fetch the operation's parameter definitions
  from the Swagger and collect values interactively.
- For each **required** parameter:
  - **Dynamic values** (`x-ms-dynamic-values`, `x-ms-dynamic-list`, `x-ms-dynamic-tree`):
    fetch from the API and present ALL items as choices. **STOP and wait for selection.**
  - **Static enum**: present enum values as choices. **STOP and wait.**
  - **Free-form with obvious default** (e.g., `folderPath=Inbox`):
    use the default BUT inform the user: "Using `Inbox` — let me know if you want different."
  - **Free-form with no obvious default**: ask the user. **STOP and wait.**
- Common examples:
  - Email trigger: `folderPath` → default `Inbox` (inform user), `subjectFilter` → optional (skip unless user mentioned)
  - SharePoint trigger: `siteUrl` → **dynamic list** (STOP, let user pick), `listName` → **dynamic list** (STOP)
  - OneDrive trigger: `folderPath` → **dynamic tree** (STOP, let user pick)
- Build the parameters list:
  ```python
  parameters = [
      {'name': 'folderPath', 'value': 'Inbox'},
      {'name': 'subjectFilter', 'value': 'Feedback'},
  ]
  ```

**Stop and wait for the user's answers before continuing.**

### Step 7B: Sandbox target (triggers)
Ask the user:
- "Do you have an existing sandbox, or should I create a new one?"
- If **existing**: ask for sandbox ID + sandbox group name.
- If **new**: **Prefer reusing an existing sandbox group** — list available groups:
  ```bash
  az sandbox group list -g {rg} -o table
  ```
  Offer them as choices. Creating a new sandbox group requires data plane propagation
  that can take **5–20+ minutes** in some regions.

  If a new group is truly needed:
  ```bash
  az sandbox group create -g {rg} -n {sg} -l {location} --identity SystemAssigned -o json
  # Extract: .identity.principalId
  ```

  Then create the sandbox with aggressive retry (propagation can take 5-20 min):
  ```bash
  az sandbox create -g {rg} -s {sg} --disk ubuntu -o json
  # If SandboxGroupNotFound, wait 30s and retry (up to 12 attempts with increasing waits)
  ```

  Wait for Running state:
  ```bash
  az sandbox show -g {rg} -s {sg} -n {sandbox_id} --query "state" -o tsv
  # Repeat until: Running
  ```

  > **⚠️ Identity (principalId) is on the sandbox GROUP, not individual sandboxes.**
  > Use the group's `principalId` for access policies.
  > If the group was created without identity, patch it:
  > ```bash
  > az sandbox group update -g {rg} -n {sg} --identity SystemAssigned -o json
  > ```
- Ask for the **callback type**:
  - **ShellCommand** — run a shell command when the trigger fires (e.g., `python /app/handler.py`)
  - **ExecuteCommand** — run a command directly without a shell (e.g., `python` with args)
  - **InvokePort** — POST to an HTTP port on the sandbox (e.g., port 5000, path `/webhook`)

**Stop and wait for the user's selection before continuing.**

### Step 8B: Create trigger config + access policy + role assignment

> **⚡ These three operations are independent — run them in parallel:**
> 1. Trigger creation
> 2. Access policy (gateway MI → connection)
> 3. Role assignment (gateway MI → sandbox group) — for ShellCommand/ExecuteCommand only

- Create the trigger config:

  > **⚠️ PowerShell JSON quoting**: Single-quoted JSON with inner double quotes
  > **PowerShell JSON quoting**: Always use `'{\"key\":\"value\"}'` (escaped quotes
  > inside single quotes). Plain `'{"key":"value"}'` will fail — PowerShell strips
  > the inner double quotes before passing to the CLI.

  ```powershell
  # For ShellCommand target:
  az connectorgateway trigger create -g {rg} --gateway {gw} -n {trigger_name} `
    --connector-name office365 --connection-name {conn} `
    --operation-name OnNewEmailV3 `
    --sandbox-id {sandbox_id} -s {sandbox_group} `
    --command "python /app/handler.py" `
    --parameters '[{\"name\": \"folderPath\", \"value\": \"Inbox\"}]' -o json

  # For InvokePort target:
  az connectorgateway trigger create -g {rg} --gateway {gw} -n {trigger_name} `
    --connector-name office365 --connection-name {conn} `
    --operation-name OnNewEmailV3 `
    --sandbox-id {sandbox_id} -s {sandbox_group} `
    --port 5000 --port-path /webhook `
    --parameters '[{\"name\": \"folderPath\", \"value\": \"Inbox\"}]' -o json
  ```
- Create the access policy granting the gateway MI access to the connection:
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
- **If InvokePort**: also configure port auth so the gateway can call the sandbox port:
  ```bash
  az sandbox port add -g {rg} -s {sandbox_group} -n {sandbox_id} --port 5000 \
    --entra-id-object-ids {gw_principal_id}
  ```
- **If ShellCommand or ExecuteCommand**: grant the gateway MI the
  **"Dev Compute SandboxGroup Data Owner"** role (`c24cf47c-5077-412d-a19c-45202126392c`)
  on the sandbox group. This is the least-privilege data plane role that grants
  `sandboxes/exec/action`:
  ```bash
  az role assignment create \
    --assignee-object-id {gw_principal_id} \
    --assignee-principal-type ServicePrincipal \
    --role "c24cf47c-5077-412d-a19c-45202126392c" \
    --scope "/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.App/sandboxGroups/{sg}"
  ```
  > **⚠️ Do NOT use Contributor.** Use the scoped data plane role above.
  Without this, the callback returns **403**.

### Step 9B: Verify trigger is active
- Check the trigger state:
  ```bash
  az connectorgateway trigger show -g {rg} --gateway {gw} -n {trigger} --query "properties.state" -o tsv
  # Should output: Enabled
  ```
- If not enabled, wait a moment and re-check.

### Final verification checklist

**For Direct API calls (path A):**
- ✅ Gateway exists
- ✅ Connection exists and status is `Connected`
- ✅ `connectionRuntimeUrl` is available (not empty)
- ✅ Access policy exists (sandbox group MI → connection)
- ✅ Egress transform rule set on sandbox matching runtime URL host
- ✅ Egress transform uses `resource: "https://management.core.windows.net/"` and `format: "Bearer {value}"`
- ✅ Test call from sandbox to runtime URL returns expected data (no auth header needed in curl)

**For Event-driven triggers (path B):**
- ✅ Gateway exists with SystemAssigned identity
- ✅ Connection exists and status is `Connected`
- ✅ Trigger config exists and state is `Enabled`
- ✅ Access policy exists (gateway MI → connection)
- ✅ RBAC: Gateway MI has "Dev Compute SandboxGroup Data Owner" role on sandbox group (for ShellCommand/ExecuteCommand)
- ✅ Sandbox is Running (for InvokePort targets)
- ✅ Port auth is configured (for InvokePort targets — gateway principalId in objectIds)
- ✅ If handler calls runtime URL: egress transform + sandbox MI ACL also needed (same as path A)

> **🚫 After trigger creation, proceed to deploying the handler app.**
> Do NOT create MCP configs — the sandbox runs without an agent, so MCP tools
> cannot be consumed. The handler calls connection runtime URLs directly
> using `requests`/`urllib` with egress transform auth.

**IMPORTANT: Do NOT skip to code generation. Walk the user through each step interactively.**

## Install

### As plugin (coding agents)
```bash
# Copilot CLI
/plugin marketplace add Azure-Samples/azure-container-apps-sandboxes
/plugin install azure-connectorgateway@Azure-Container-Apps

# Claude Code
claude plugin add Azure-Samples/azure-container-apps-sandboxes
```

### az CLI extension
```bash
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "az_cli_connectorgateway-*-py3-none-any.whl" --dir /tmp
az extension add --source /tmp/az_cli_connectorgateway-*-py3-none-any.whl
```

### Python SDK
```bash
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_connectorgateway-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_connectorgateway-*-py3-none-any.whl
```

### Uninstall
```bash
az extension remove --name trigger
pip uninstall azure-connectorgateway
```

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Connector Gateway (ARM: Microsoft.Web/connectorGateways)     │
│                                                               │
│  ├── Connections (OAuth-authorized connector access)          │
│  ├── Trigger Configs (event subscription → callback)          │
│  └── Access Policies (MI → connection access)                 │
│                                                               │
│  Trigger Config subscribes to connector events and            │
│  POSTs notifications to the sandbox callback URL              │
└──────────────────────────────────────────────────────────────┘
           │                                │
           │ connector event fires          │ callback to sandbox
           ▼                                ▼
┌─────────────────────┐    ┌──────────────────────────────────┐
│  Connector           │    │  Sandbox (KVM µVM)                │
│  (Office 365, GitHub │    │  executeShellCommand (command)    │
│   Blob Storage, etc) │    │  or proxy port (InvokePort)       │
└─────────────────────┘    └──────────────────────────────────┘
```

## Python SDK

```python
from azure.connectorgateway import ConnectorGatewayClient, TriggerClient
from azure.sandbox import SandboxClient

conn_client = ConnectorGatewayClient(resource_group="my-rg")
trigger_client = TriggerClient(resource_group="my-rg")
sbx_client = SandboxClient(resource_group="my-rg")

# Create trigger with ShellCommand target (shell-interpreted command)
trigger = trigger_client.create_trigger("my-gw", "email-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id="my-sandbox-id",
    sandbox_group="my-sg",
    command="python /app/handle_email.py",
    parameters=[{"name": "folderPath", "value": "Inbox"}])

# Create trigger with ExecuteCommand target (direct exec, no shell)
trigger = trigger_client.create_trigger("my-gw", "cmd-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id="my-sandbox-id",
    sandbox_group="my-sg",
    execute_command="python",
    execute_args=["/app/handle_email.py", "--verbose"],
    parameters=[{"name": "folderPath", "value": "Inbox"}])

# Create trigger with InvokePort target (HTTP call to sandbox port)
trigger = trigger_client.create_trigger("my-gw", "webhook-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id="my-sandbox-id",
    sandbox_group="my-sg",
    port=5000,
    port_path="/webhook",
    parameters=[{"name": "folderPath", "value": "Inbox"}])

# Lifecycle
trigger_client.disable_trigger("my-gw", "email-handler")
trigger_client.enable_trigger("my-gw", "email-handler")
trigger_client.delete_trigger("my-gw", "email-handler")
```

Run `help(trigger_client)` to see all available methods.

## az CLI

```bash
az connectorgateway trigger list -g my-rg --gateway my-gw
az connectorgateway trigger create -g my-rg --gateway my-gw -n email-handler \
  --connector-name office365 --connection-name o365-conn \
  --operation-name OnNewEmail \
  --sandbox-id my-sandbox-id -s my-sandbox-group \
  --port 5000 --port-path /webhook
az connectorgateway trigger enable -g my-rg --gateway my-gw -n email-handler
az connectorgateway trigger disable -g my-rg --gateway my-gw -n email-handler
az connectorgateway trigger operations list -g my-rg --gateway my-gw --connector-type office365
az connectorgateway trigger delete -g my-rg --gateway my-gw -n email-handler
```

Run `az connectorgateway --help` to see all available commands.

## Key Concepts

| Concept | What it is |
|---------|-----------|
| **Trigger Config** | ARM resource on the gateway (`connectorGateways/{gw}/triggerConfigs/{name}`). Subscribes to connector events and delivers to sandbox callback URL |
| **Callback URL** | ADC sandbox endpoint or proxy port URL — built automatically by the SDK |
| **Access Policy** | Grants the gateway MI access to the connection (required for event subscription) |
| **ShellCommand target** | Callback URL = `{ADC}/.../{sandboxId}/executeShellCommand`; body = `{command, shell, ...}`. Shell-interpreted command string. |
| **ExecuteCommand target** | Callback URL = `{ADC}/.../{sandboxId}/executeCommand`; body = `{command, args, ...}`. Direct exec, no shell. |
| **InvokePort target** | Callback URL = `https://{sandboxId}--{port}.proxy.azuredevcompute.io/...`. HTTP call to sandbox port. |

## Trigger Operations

| Type | How it works | Example |
|------|-------------|---------|
| **batch** | Periodic polling check | "When a file is created or modified" |
| **single** | Real-time notification | "When a new email arrives" |

## Gotchas

| Issue | Solution |
|-------|----------|
| Trigger not firing | Ensure access policy exists granting gateway MI access to the connection |
| Gateway can't subscribe | Create an access policy granting the gateway MI access to the connection |
| Sandbox must be Running | For InvokePort targets, sandbox must be running; for ShellCommand, sandbox activates on demand |
| Port auth for InvokePort | Add gateway's principalId to the port's entraId objectIds on the sandbox |
| Cleanup order | Delete trigger config → connection → sandbox → gateway |
| SandboxGroupNotFound 404 | Data plane propagation after ARM group creation can take **5–20+ minutes** in some regions (especially `brazilsouth`). Use retry with 30-140s waits, up to 12 attempts. **Better: reuse existing sandbox groups** — `client.list_groups()` to find propagated groups |
| Sandbox state field wrong path | State is at `sbx['state']` (top level), NOT `sbx['properties']['state']` — the data plane API returns flat JSON |
| Sandbox identity not found | Identity (principalId/tenantId) is on the **sandbox group**, not individual sandboxes. Use `group['identity']['principalId']`. Create group with `identity={"type": "SystemAssigned"}` |
| `dynamicInvoke` 400: `parameters` not valid | Use `{"request": {"method": ..., "path": ...}}` format, NOT `{"parameters": {"operationId": ...}}`. The operationId format is not supported by this endpoint |
| `dynamicInvoke` 400: `Content-*` headers not supported | Do NOT include `Content-Type` or other `Content-*` headers in the request object — the API rejects them |
| `dynamicInvoke` returns `NotFound` for POST | Ensure you pass `queries` and `body` in the request object. The `az rest` body must include the full `{"request": {"method": ..., "path": ..., "queries": ..., "body": ...}}` structure |
| `list_operations` AttributeError | Use `az rest --method POST .../{gw}/listOperations --body '{"connectorName": "..."}'` or `az connectorgateway trigger operations list` |
| Runtime URL 403: missing connection ACL | Create an access policy granting the caller's principalId access to the connection before calling the runtime URL directly |
| Consent redirect page shows error | Use `--redirect-url "https://microsoft.com"` instead of the default consent service redirect. The consent auto-confirms at `/confirm` — the redirect is just for UX. User sees microsoft.com after auth instead of an error page. |
| Connection stuck in "Error" after consent | Check status with `az connectorgateway connection show`. If still `Error`, the user may not have completed browser auth. Re-generate the consent link and retry. |
| `dynamicInvoke` browse fails with mangled JSON | Use `@file` pattern for `az rest --body` when IDs contain `!` or special chars. Write JSON to temp file, pass `--body @$tmpFile`. Always URL-encode IDs with `[System.Uri]::EscapeDataString()` |
| Swagger paths include `/{connectionId}/...` | Strip the `/{connectionId}` prefix when building `dynamicInvoke` paths — the connection context is already set by the endpoint |
| ShellCommand trigger 403 on callback | Gateway MI needs **"Dev Compute SandboxGroup Data Owner"** role (`c24cf47c-5077-412d-a19c-45202126392c`) on the sandbox group. Do NOT use Contributor — use this least-privilege data plane role |

## Handler Development Guide

When the user asks to "build an app" or "create a handler", you create a Python script
that gets deployed to the sandbox. This section covers critical details that avoid
common pitfalls.

### ⚠️ CRITICAL: Collect ALL handler parameters from the user BEFORE writing code

> **Do NOT hardcode folder paths, channel IDs, site URLs, list names, or any
> connector-specific values in the handler.** These MUST be collected from the user
> by fetching dynamic values from the connector API.

Before writing ANY handler code, identify every connector-specific value the handler
will use (target folders, channels, lists, etc.) and collect them interactively:

| Handler needs... | How to collect |
|-----------------|----------------|
| OneDrive folder path | Fetch folders via `dynamicInvoke` GET `/datasets/default/folders`, present as choices |
| SharePoint site | Fetch sites via `dynamicInvoke` GET `/datasets`, present as choices |
| SharePoint list | Fetch lists via `dynamicInvoke` GET `/datasets/{site}/tables`, present as choices |
| Teams team/channel | Fetch via `dynamicInvoke` GET `/beta/me/joinedTeams`, then channels, present as choices |
| Email folder | Fetch via `dynamicInvoke` GET `/datasets/default/folders`, or use default `Inbox` (inform user) |

**Example: Collecting OneDrive folder before writing handler (tree browsing):**
```powershell
# Step 1: Fetch ROOT folders (open operation)
az rest --method POST `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{onedrive_conn}/dynamicInvoke?api-version=2026-05-01-preview" `
  --body '{\"request\":{\"method\":\"GET\",\"path\":\"/datasets/default/folders\"}}' `
  --headers "Content-Type=application/json" `
  --query "response.body[].{Name:DisplayName, Id:Id, Path:Path}" -o table
# → Present as choices via ask_user. STOP and wait.

# Step 2: If user wants to go deeper — BROWSE into the selected folder
$selectedId = "<Id from the folder the user picked>"
$encodedId = [System.Uri]::EscapeDataString($selectedId)
$bodyJson = '{"request":{"method":"GET","path":"/datasets/default/folders/' + $encodedId + '"}}'
$tmpBody = [System.IO.Path]::GetTempFileName()
$bodyJson | Out-File -FilePath $tmpBody -Encoding ascii -NoNewline

az rest --method POST `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{onedrive_conn}/dynamicInvoke?api-version=2026-05-01-preview" `
  --body "@$tmpBody" `
  --headers "Content-Type=application/json" `
  --query "response.body[].{Name:DisplayName, Id:Id, Path:Path}" -o table

Remove-Item $tmpBody -ErrorAction SilentlyContinue
# → Present child folders + "✅ Select this folder" option. STOP and wait.
# → Repeat until user selects a folder. Use the Path value in the handler.
```

**STOP and wait for the user's selection. Only THEN write the handler code with
the selected values.**

> **Key rule**: If a value in the handler represents a connector resource
> (folder, list, channel, site, mailbox), it is a dynamic value. Fetch it
> from the connector API and let the user choose — do NOT assume or hardcode.

### How event data reaches the handler — depends on target type

> **⚠️ CRITICAL: Know which target type you are using.**
> The trigger's target type determines whether event data is passed to the handler.

| Target type | How event data is delivered | Handler approach |
|-------------|---------------------------|-----------------|
| **InvokePort** (`--port 8080 --port-path /webhook`) | Event data IS included in the POST body to the port | Parse the request body (e.g., Flask `request.json`) — the trigger payload contains the event data directly |
| **ShellCommand** (`--command "python /app/handler.py"`) | Event data is **NOT** passed. Body only contains `{"command": "...", "activationMode": "OnDemand"}` | Handler must **fetch the data itself** by calling the connection runtime URL |
| **ExecuteCommand** (`--execute-command "python"`) | Same as ShellCommand — event data is NOT passed | Same as ShellCommand — handler must fetch data itself |

**How to determine your target type:**
- If you used `--port` and `--port-path` → **InvokePort** (event data in POST body)
- If you used `--command` → **ShellCommand** (must fetch data yourself)
- Check the trigger's `callbackUrl`: if it contains `proxy.azuredevcompute.io` → InvokePort;
  if it contains `executeShellCommand` → ShellCommand

### Sandbox environment details

| Feature | Details |
|---------|---------|
| **Managed Identity** | App Service-style (NOT IMDS). Use `IDENTITY_ENDPOINT` + `IDENTITY_HEADER` env vars |
| **Python HTTP library** | Use `requests` or `urllib`. `httpx` has SSL issues in sandboxes |
| **stdin** | Empty (length 0) — cannot pass data via stdin |
| **Environment variables** | Work via `executeShellCommand`'s `environment` field |
| **Auth for runtime URL calls** | NOT needed — egress transform injects Bearer token automatically |
| **File system** | Writable at `/app/`. Deploy handler scripts here |

### MI token in sandbox (App Service-style)

Sandboxes do NOT use IMDS (`169.254.169.254`). They use the App Service MI pattern:
```python
import os, requests

def get_mi_token(resource):
    endpoint = os.environ["IDENTITY_ENDPOINT"]
    header = os.environ["IDENTITY_HEADER"]
    resp = requests.get(
        f"{endpoint}?resource={resource}&api-version=2019-08-01",
        headers={"X-IDENTITY-HEADER": header})
    return resp.json()["access_token"]
```
> **⚠️ You usually don't need MI tokens in handlers.** The egress transform
> injects auth on runtime URL calls automatically. MI is only needed for calling
> other Azure services (e.g., Azure Storage directly).

### O365 connector quirks (email handlers)

| Quirk | Workaround |
|-------|-----------|
| `HasAttachment` field is singular | Use `HasAttachment` not `HasAttachments` in filters |
| `hasAttachments=true` query filter is unreliable | The server-side filter is inconsistent. Fetch top N emails and filter client-side for ones with actual attachments |
| `includeAttachments=true` doesn't always return `ContentBytes` | API is intermittent — add **retry logic** (3 attempts with 2s delay) |
| Separate attachment endpoint (`/v2/Mail/{id}/Attachments/{id}`) | Returns 404 — do NOT use. Use `/codeless/v1.0/me/messages/{id}/attachments/{id}` instead |
| `Attachments` array has `contentBytes: null` without `includeAttachments=true` | Always pass `includeAttachments=true` in query |
| Inline images count as attachments | Filter with `not att.get("IsInline", False)` to skip them |

### Handler template (ShellCommand + runtime URL)

```python
#!/usr/bin/env python3
"""Handler template for ShellCommand triggers calling connection runtime URLs."""
import os, time, json, requests

# Runtime URLs — egress transform handles auth, NO Bearer token needed
O365_URL = os.environ.get("O365_RUNTIME_URL", "https://....azure-apihub.net/apim/office365/...")
ONEDRIVE_URL = os.environ.get("ONEDRIVE_RUNTIME_URL", "https://....azure-apihub.net/apim/onedriveforbusiness/...")

def http_get(url, retries=3, delay=2):
    """GET with retry — connector API can be intermittent."""
    for attempt in range(retries):
        resp = requests.get(url, verify=False)
        if resp.status_code == 200:
            return resp.json()
        if attempt < retries - 1:
            time.sleep(delay)
    return None

def http_post(url, data=None, json_body=None, content_type="application/json"):
    """POST to runtime URL."""
    headers = {"Content-Type": content_type}
    if json_body:
        return requests.post(url, json=json_body, headers=headers, verify=False)
    return requests.post(url, data=data, headers=headers, verify=False)

def main():
    # 1. Fetch data from source connector
    # 2. Process / transform
    # 3. Write to destination connector
    pass

if __name__ == "__main__":
    main()
```

> **⚠️ Key points for handler scripts:**
> - Use `verify=False` in requests — sandbox may lack CA certs
> - Add retry logic for API calls (2-3 attempts, 2s delay)
> - Egress handles auth — do NOT add Authorization headers yourself
> - Use `requests` not `httpx` (SSL issues in sandbox)
> - The handler script IS a file (`/app/handler.py`) — this is the "app", not a setup script
> - Deploy via `executeShellCommand`: `echo '<base64>' | base64 -d > /app/handler.py`

## Labs

See [labs/README.md](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/README.md) for trigger labs.

## References

- [prerequisites.md](references/prerequisites.md)
- [quickstart.md](references/quickstart.md)
- [trigger-flow.md](references/trigger-flow.md)
