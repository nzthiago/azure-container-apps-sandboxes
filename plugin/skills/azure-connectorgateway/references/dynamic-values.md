# Dynamic Values & Schema Resolution

How to resolve connector parameters that require dynamic API calls.

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

## `x-ms-dynamic-list` — Same as dynamic-values with nesting

Identical to `x-ms-dynamic-values` except:
- The `operationId` may be nested: check `dynamicState.operationId` or
  `dynamicState.extension.operationId` if direct `operationId` is missing
- Supports both `value-path` and `valuePath` (camelCase variant)
- Handle exactly the same way as `x-ms-dynamic-values`

## `x-ms-dynamic-tree` — Hierarchical browsing (folder tree)

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

### Step-by-step algorithm:

**Step T1: Resolve the `open` operationId to an HTTP path.**
Find the operation in the Swagger (from `listOperations`) whose `operationId`
matches `open.operationId`. Extract its `method` and `path`.

**Step T2: Call the `open` operation to get root items.**
```powershell
# For static paths (no dynamic IDs), use escaped quotes:
az rest --method POST `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/dynamicInvoke?api-version=2026-05-01-preview" `
  --body '{\"request\":{\"method\":\"GET\",\"path\":\"/datasets/default/folders\"}}' `
  --headers "Content-Type=application/json" `
  --query "response.body[].{Name:DisplayName, Id:Id, IsFolder:IsFolder}" -o table
```

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
$selectedId = "b!oBRIc...01EBKFNYMT34SLMMPFYFEKV2L46DV54RIE"
$encodedId = [System.Uri]::EscapeDataString($selectedId)

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

**Step T5: Present children + selection option. STOP and wait.**

**Step T6: Repeat T4-T5** until the user selects a folder or leaf item.
Use the final item's `Path` or `Id` as the parameter value.

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
- The final value to use is typically the `Path` field or the `Id` field

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
