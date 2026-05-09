# Dynamic Values & Schema Resolution

How to resolve connector parameters that require dynamic API calls.

## Step 1: Get the connector's Swagger (REQUIRED FIRST)

Before resolving any dynamic value, fetch the connector's **full Swagger definition**.
This gives you operationId → HTTP method + path mappings for all operations.

```powershell
# Fetch the full Swagger — MUST save to file first (ConvertFrom-Json fails on piped output)
az rest --method GET `
  --url "https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Web/locations/{location}/managedApis/{connector}" `
  --url-parameters "api-version=2016-06-01" "export=true" -o json > $env:TEMP\swagger.json

# Parse the swagger and extract operationId → path table
python -c "
import json
with open(r'$env:TEMP\swagger.json') as f:
    data = json.load(f)
paths = data.get('properties',{}).get('apiDefinitions',{}).get('value',{}).get('paths',{})
for path, methods in paths.items():
    for method, details in methods.items():
        if isinstance(details, dict) and 'operationId' in details:
            clean_path = path.replace('/{connectionId}', '')
            print(f'{details[\"operationId\"]:40s} {method.upper():6s} {clean_path}')
"
```

> **⚠️ PowerShell parsing issue:** `az rest` with `export=true` returns raw swagger that
> breaks `ConvertFrom-Json` when piped. Always save to file with `-o json > file.json` first.

**To find the path for an operationId** (e.g., `GetFolders`):
- Look through `paths` → each path key (e.g., `/{connectionId}/datasets/default/folders`) has method entries (get, post, etc.)
- Each method entry has an `operationId` field
- When you find the matching `operationId`, the path key (minus `/{connectionId}`) is what you pass to `dynamicInvoke`

> **Strip `/{connectionId}` from the path.** The Swagger paths start with `/{connectionId}/...`
> but when calling `dynamicInvoke`, use only the part after `/{connectionId}`.
> Example: Swagger path `/{connectionId}/datasets/default/folders` → dynamicInvoke path `/datasets/default/folders`

**⚠️ Literal path segments gotcha:** Some paths look like variables but are literal strings.
Example: `/notebooks/notebookKey/sections` — `notebookKey` is a **literal** path segment, NOT a
variable to substitute. The actual notebook key goes as a **query parameter** named `notebookKey`.
Always check the Swagger parameter definitions (`in: query` vs `in: path`) to know which is which.

### Common connector paths (quick reference)

| Connector | operationId | Path | Key params |
|-----------|-------------|------|------------|
| office365 | SendMailV2 | `/v2/Mail` | body: To, Subject, Body |
| office365 | GetEmails | `/datasets/default/messages` | folderPath (query) |
| office365 | OnNewEmailV3 | `/trigger1/datasets/default/messages` | folderPath (query) |
| onedriveforbusiness | ListFolder | `/datasets/default/folders/{id}/listchildren` | id (path) |
| onedriveforbusiness | CreateFile | `/datasets/default/files` | folderPath (query), name (query) |
| sharepointonline | GetItems | `/datasets/{dataset}/tables/{table}/items` | dataset, table (path) |
| sharepointonline | PostItem | `/datasets/{dataset}/tables/{table}/items` | dataset, table (path) |
| teams | GetAllTeams | `/beta/me/joinedTeams` | — |
| teams | GetChannels | `/beta/groups/{groupId}/channels` | groupId (path) |
| onenote | GetNotebooks | `/notebooks` | — |
| onenote | GetSectionsInNotebook | `/notebooks/notebookKey/sections` | notebookKey (query, NOT path) |
| onenote | OnNewPageInSection | `/trigger3/sections/Dynamic/pages` | notebookKey, sectionId (query) |

## Step 2: Understand value vs display name

When resolving dynamic values, every item in the response has TWO fields:
- **value** (from `value-path`) — the ID/key to pass to APIs. Example: `"b!oBRIcPVy5ke..."`
- **display name** (from `value-title`) — what to show the user. Example: `"Documents"`

**CRITICAL rules:**
1. Show the **display name** to the user for selection
2. Store the **value** internally
3. When a subsequent parameter depends on this one, pass the **value** (NEVER the display name)

Example dependency chain:
```
Parameter 1: "notebookKey" (x-ms-dynamic-values → operationId: "GetNotebooks")
  → API returns: [{Name: "Work Notebook", Key: "Aprana @ Microsoft|$|https://..."}]
  → Show user: ["Work Notebook", "Personal Notes"]
  → User picks "Work Notebook" → STORE value = "Aprana @ Microsoft|$|https://..."

Parameter 2: "sectionId" (x-ms-dynamic-values → operationId: "GetSectionsInNotebook", parameters: {"notebookKey": {"parameter": "notebookKey"}})
  → Call GetSectionsInNotebook with notebookKey = STORED VALUE (the long key, NOT "Work Notebook")
  → API returns: [{Name: "Meeting Notes", Id: "section-id-123"}]
  → Show user: ["Meeting Notes", "Ideas"]
  → User picks "Meeting Notes" → STORE value = "section-id-123"
```

> **⚠️ The most common agent mistake:** Using the display name ("Work Notebook") instead of the
> stored value ("Aprana @ Microsoft|$|https://...") when calling the next operation.
> This causes 404s or empty results because the API expects the ID/key, not the human-readable name.

## `x-ms-dynamic-values` — Flat list of options

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
1. Resolve `operationId` → find the matching path in the Swagger (fetched in Step 1):
   - Search through `paths` for an entry whose method has `operationId` matching the one in the extension
   - Extract the path key and strip `/{connectionId}` prefix
   - Extract the HTTP method (get, post, etc.)
   ```
   Example: operationId "GetFolders" found at path "/{connectionId}/datasets/default/folders" with method "get"
   → dynamicInvoke path = "/datasets/default/folders", method = "GET"
   ```
2. Resolve `parameters` — substitute **values** (NOT display names) from previously collected params:
   - `{"parameter": "dataset"}` → use the **stored value** the user selected for `dataset` (the `value-path` field, NOT the display name)
   - `{"value": "default"}` → use literal `"default"`
   - Plain string → use as-is
3. Call `dynamicInvoke`:
   ```powershell
   az rest --method POST `
     --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" `
     --body '{\"request\":{\"method\":\"GET\",\"path\":\"{resolved_path}\"}}' `
     --headers "Content-Type=application/json" -o json
   ```
   > **PowerShell JSON quoting tips:**
   > - Simple static: `'{\"request\":{\"method\":\"GET\",\"path\":\"/datasets\"}}'`
   > - Dynamic values with special chars (`!`, `'`, spaces): write to temp file, use `--body @$tmpFile`
   > ```powershell
   > $body = @{request=@{method="GET";path="/datasets/default/files/$encodedId"}} | ConvertTo-Json -Depth 5 -Compress
   > $tmpFile = New-TemporaryFile; Set-Content $tmpFile $body
   > az rest --method POST --url "..." --body "@$tmpFile" --headers "Content-Type=application/json" -o json
   > Remove-Item $tmpFile
   > ```

4. Unwrap response: extract `response.body`
5. If `value-collection` is set (e.g., `"value"`), navigate to that array: `response.body.value`
6. For each item: extract TWO things:
   - `value-path` field (e.g., `Id`) → **this is the VALUE to store and pass to subsequent API calls**
   - `value-title` field (e.g., `DisplayName`) → **this is what you show the user**
7. Present `value-title` items as choices via `ask_user`
8. **STOP and wait for user selection**
9. **Store the selected item's `value-path` field** — you will need it if any subsequent parameter depends on this one

> **⚠️ NEVER use the display name in API calls.** If the next parameter depends on this one
> (via `{"parameter": "thisParam"}`), pass the stored VALUE, not what the user saw.

**Common examples:**
| Connector | Parameter | Path | value-path | value-title |
|-----------|-----------|------|-----------|-------------|
| OneDrive | `folderPath` | `/datasets/default/folders` | `Path` | `DisplayName` |
| Teams | `groupId` | `/beta/me/joinedTeams` | `id` | `displayName` |
| SharePoint | `dataset` | `/datasets` | `Url` | `Name` |
| Office365 | `folderPath` | `/datasets/default/folders` | `Path` | `DisplayName` |

## `x-ms-dynamic-list` — Same as dynamic-values with nesting

Identical to `x-ms-dynamic-values` except:
- The `operationId` may be nested: check `dynamicState.operationId` or
  `dynamicState.extension.operationId` if direct `operationId` is missing
- Supports both `value-path` and `valuePath` (camelCase variant)
- Handle exactly the same way as `x-ms-dynamic-values`

## `x-ms-dynamic-tree` — Hierarchical browsing (folder tree)

Used for folder pickers where the user navigates level-by-level. The extension defines:
- `open` — fetches root-level items
- `browse` — fetches children of a selected item
- `settings` — controls what can be selected

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

### Algorithm

**Step T1: Get the `open` operation path from the Swagger.**
```
open.operationId = "ListRootFolders"
→ Find in Swagger paths → "/{connectionId}/datasets/default/folders" (GET)
→ dynamicInvoke path = "/datasets/default/folders"
```

**Step T2: Call `open` via dynamicInvoke.**
```powershell
$body = @{request=@{method="GET";path="/datasets/default/folders"}} | ConvertTo-Json -Compress
$tmp = New-TemporaryFile; Set-Content $tmp $body
az rest --method POST `
  --url ".../{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" `
  --body "@$tmp" -o json > $env:TEMP\tree-response.json
Remove-Item $tmp
```

**Step T3: Parse the response and present items.**
- Navigate to `response.body` → then follow `itemsPath` (e.g., `"value"` → `response.body.value`)
- For each item, extract:
  - `itemTitlePath` field (e.g., `DisplayName`) → show to user
  - `itemValuePath` field (e.g., `Id`) → store internally
  - Check `itemIsParent` condition → mark with 📁 if true (can browse deeper)

Present to user:
```
📁 Apps
📁 Documents
📁 Desktop
📁 EmailAttachments
✅ Select root (/)      ← only show if canSelectParentNodes is true
```
**STOP and wait for user selection.**

**Step T4: User selects a folder → BROWSE deeper.**

1. Get the `browse` operation path from the Swagger:
   ```
   browse.operationId = "ListChildFolders"
   → Found in Swagger → "/{connectionId}/datasets/default/folders/{id}" (GET)
   → dynamicInvoke path template = "/datasets/default/folders/{id}"
   ```

2. Substitute the selected item's value into the path using `browse.parameters`:
   ```
   browse.parameters = { "id": { "selectedItemValuePath": "Id" } }
   
   This means: take the "Id" field from the item the user selected,
   and substitute it for {id} in the path.
   ```

3. **URL-encode the value** — IDs often contain `!`, `/`, spaces, and other special chars:
   ```powershell
   $selectedId = "b!oBRIcPVy5ke..."  # The stored VALUE from user's selection
   $encodedId = [System.Uri]::EscapeDataString($selectedId)
   $browsePath = "/datasets/default/folders/$encodedId"
   ```

4. Call dynamicInvoke with the constructed browse path:
   ```powershell
   $body = @{request=@{method="GET";path=$browsePath}} | ConvertTo-Json -Compress
   $tmp = New-TemporaryFile; Set-Content $tmp $body
   az rest --method POST `
     --url ".../{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" `
     --body "@$tmp" -o json > $env:TEMP\tree-response.json
   Remove-Item $tmp
   ```

5. Parse response same as Step T3 — present child items to user.

**Step T5: Present children. STOP and wait.**
```
📁 Project Files
📁 Templates
📄 README.md
✅ Select "Documents"    ← show if canSelectParentNodes is true
```

**Step T6: Repeat T4-T5** until user selects (clicks ✅) or picks a leaf item.

### Final value to use

When the user makes their final selection:
- Use the `itemValuePath` field (e.g., `Id` or `Path`) as the parameter value
- Some connectors expect `Path` (e.g., `/Documents/Copilot`), others expect `Id`
- Check the Swagger parameter definition for the original parameter to see what type it expects

### Key rules for dynamic tree

- **Always use `@file` pattern** for browse calls (IDs have special chars that break inline JSON)
- **Always URL-encode** values with `[System.Uri]::EscapeDataString()`
- **Always STOP at each level** — let the user choose to go deeper or select current
- **Use VALUE not display name** when constructing browse paths
- If `browse` is not defined in the extension, reuse `open` with the selected item's value as a parameter
- Check `itemIsParent` to know which items can be browsed deeper (📁) vs are leaf items (📄)

## `x-ms-dynamic-schema` — Schema depends on prior selection

The parameter's available fields/columns change based on another parameter's value.
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

### Step-by-step algorithm:

**Step S1: Identify the dependency chain.**
Each entry like `"dataset": { "parameter": "dataset" }` means the schema operation
needs the value the user already selected for `dataset`.

Example dependency chain for SharePoint `PostItem`:
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
# → Present sites as choices. STOP and wait.

# Step S2b: Get lists for the selected site (table parameter)
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
# → Present lists as choices. STOP and wait.
```

**Step S3: Resolve the schema operation's path.**
Find the operation whose `operationId` matches. Substitute collected param values.
```
GetTable → GET /$metadata.json/datasets/{dataset}/tables/{table}
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
Split on `/` and walk each key: `Schema/Items` → `response.body.Schema.Items`

Example result:
```json
{
  "type": "object",
  "properties": {
    "Title": { "type": "string", "x-ms-display": "Title" },
    "StartDate": { "type": "string", "format": "date", "x-ms-display": "Start Date" },
    "Manager": { "type": "string", "x-ms-display": "Manager" }
  },
  "required": ["Title"]
}
```

**Step S6: Present fields to user. STOP and wait.**
```
Available columns in "New Hires" list:
• Title (string) — REQUIRED
• StartDate (date)
• Manager (string)
Which fields do you want to populate, and what values?
```

**Step S7: Build the body with user-provided values.**

**Key rules for dynamic schema:**
- **Collect dependencies in order** — schema operation needs values from prior selections
- **Always resolve `operationId` to path** from the Swagger
- **Navigate `value-path`** by splitting on `/` and walking each key
- **Present ALL fields** with types and required markers
- **Do NOT assume field values** — always ask the user
- **URL-encode** all path parameters

## No extension — static enum or free-form

- If the parameter has a **static enum**, present values as choices. **STOP and wait.**
- If the parameter is **free-form**, ask the user directly. **STOP and wait.**

## Response unwrapping

The `dynamicInvoke` response is always double-wrapped:
```json
{"response": {"statusCode": "OK", "body": { ...actual data... }, "headers": {...}}}
```
Always extract from `response.body`. Use `--query "response.body"` with `az rest`.

## When to STOP vs. use defaults

| Parameter type | Action |
|---------------|--------|
| Any `x-ms-dynamic-*` extension | **Always STOP** — fetch, present choices, wait for user |
| Static enum from Swagger | **Always STOP** — present choices, wait for user |
| Free-form with obvious default (e.g., `folderPath=Inbox`) | Use default BUT tell user |
| Free-form with no obvious default | **Always STOP** — ask the user |
| Optional parameters | **Skip** unless user mentioned them |
