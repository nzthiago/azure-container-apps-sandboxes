# Egress Transform & Access Policy Setup

How to configure a sandbox to call connection runtime URLs directly.

> **⚠️ Egress setup uses `az rest` against the ADC data plane.**
> The Python SDK (`SandboxClient`) is not shipped with the current CLI release.
> Use the `az rest` commands below instead.

## Overview

When a sandbox app calls a connection runtime URL, it needs:
1. **Access policy (ACL)** — grants the sandbox group MI permission to use the connection
2. **Egress transform rule** — automatically injects Bearer token on outbound HTTPS calls

After setup, the sandbox app calls the runtime URL with **no auth header** — the
platform handles it transparently.

## Step 1: Create access policy for sandbox MI

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

## Step 2: Get the connection runtime URL

```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}?api-version=2026-05-01-preview" \
  --query "properties.connectionRuntimeUrl" -o tsv
```

## Step 3: Set egress transform rule

The egress transform injects `Authorization: Bearer {token}` using the sandbox's
system-assigned managed identity. The sandbox code makes plain HTTP calls with
NO auth header — the platform handles it.

> **⚠️ The sandbox MUST be running** before setting egress policy.
> If stopped, resume it first:
> ```bash
> aca sandbox resume -g {rg} --group {sg} --id {sandbox_id}
> # Wait for Running state
> aca sandbox show -g {rg} --group {sg} --id {sandbox_id} --query "state"
> ```

**Use `az rest` to set the egress policy** (POST to ADC data plane):

```powershell
# Extract hostname from connectionRuntimeUrl
$runtimeUrl = "{connectionRuntimeUrl}"
$host = ([System.Uri]$runtimeUrl).Host

# Build egress policy body — this REPLACES all existing rules
$egressBody = @{
  defaultAction = "Allow"
  rules = @(@{
    name = "connection-auth"
    match = @{ host = $host }
    action = @{
      type = "Transform"
      headers = @(@{
        operation = "Set"
        name = "Authorization"
        valueRef = @{
          managedIdentityRef = @{
            resource = "https://management.core.windows.net/"
            format = "Bearer {value}"
            type = "SystemAssigned"
          }
        }
      })
    }
  })
} | ConvertTo-Json -Depth 8 -Compress

$tmp = New-TemporaryFile; Set-Content $tmp $egressBody
az rest --method POST `
  --url "https://management.azuredevcompute.io/subscriptions/{sub}/resourceGroups/{rg}/sandboxGroups/{sg}/sandboxes/{sandbox_id}/egresspolicy?api-version=2026-02-01-preview" `
  --body "@$tmp" `
  --resource "https://management.azuredevcompute.io/"
Remove-Item $tmp
```

> **⚠️ This is a REPLACE operation** — the policy body replaces ALL existing egress rules.
> If the sandbox already has egress rules you want to keep, GET the current policy first,
> append the new rule, then POST the combined set.

## Critical details for egress transform

| Detail | Value |
|--------|-------|
| **API endpoint** | POST to `https://management.azuredevcompute.io/.../sandboxes/{id}/egresspolicy` (lowercase, POST method) |
| **Semantics** | The POST **replaces all rules**. To preserve existing rules, GET first, append, then POST the combined set |
| **Token resource** | `https://management.core.windows.net/` — NOT `https://management.azure.com/`, NOT `https://apihub.azure.com/.default` |
| **Format** | `"Bearer {value}"` — only `{value}` works as placeholder, NOT `{token}` |
| **Match host** | Extract hostname from `connectionRuntimeUrl`. One rule covers all connections on that gateway |
| **type** | Must be `"SystemAssigned"` — the sandbox group's system MI |

## Step 4: Test the connection from inside the sandbox

After ACL + egress setup, verify with a read-only test call via `executeShellCommand`:

```bash
# No auth header needed — egress transform injects it automatically
# Use -k flag if sandbox doesn't have CA certs installed
curl -sk "${RUNTIME_URL}/{test_path}"
```

**Test calls by connector:**

| Connector | Test call (GET, read-only) | Expected result |
|-----------|---------------------------|-----------------|
| **teams** | `GET {runtimeUrl}/beta/me/joinedTeams` | JSON array of teams |
| **office365** | `GET {runtimeUrl}/v2/Mail?folderPath=Inbox&top=1` | Latest email |
| **onedriveforbusiness** | `GET {runtimeUrl}/datasets/default/folders` | Root folder list |
| **sharepointonline** | `GET {runtimeUrl}/datasets` | List of SharePoint sites |
| **azureblob** | `GET {runtimeUrl}/datasets/default/foldersV2?path=/` | Container/folder list |

**Troubleshooting:**

| Result | Cause | Fix |
|--------|-------|-----|
| Data returned ✅ | Working | — |
| `403` | ACL missing or not propagated | Wait 30s, retry. Check ACL exists |
| `401` / "AuthorizationToken required" | Egress rule wrong | Check resource is `https://management.core.windows.net/` |
| `CERTIFICATE_VERIFY_FAILED` | TLS-intercepting proxy | Use system CA: `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt` or `verify=False` for dev |

### SSL/TLS in sandbox environments

Sandboxes use a TLS-intercepting egress proxy. HTTPS calls to connector runtime URLs
(`*.azure-apihub.net`) will fail with `CERTIFICATE_VERIFY_FAILED` unless handled:

```python
# Preferred: use system CA store (includes proxy CA)
import os
os.environ['REQUESTS_CA_BUNDLE'] = '/etc/ssl/certs/ca-certificates.crt'
# Or with ssl module:
import ssl
ctx = ssl.create_default_context()
ctx.load_default_certs()

# Fallback (dev/testing only): disable verification
import ssl
ctx = ssl._create_unverified_context()
# Or: requests.get(url, verify=False)
```

Do NOT skip SSL silently in production — try system CA first, fall back to unverified only if needed.

## Two auth patterns — when to use each

| Context | Pattern | Why |
|---------|---------|-----|
| **Local setup** (interactive, fetching dynamic values) | `dynamicInvoke` via ARM | Uses your Azure CLI identity |
| **Sandbox runtime** (deployed handler) | `connectionRuntimeUrl` + egress transform | Uses sandbox MI; `dynamicInvoke` fails with `AIGatewayConnectionOwnerAccessDenied` from sandbox MI |
