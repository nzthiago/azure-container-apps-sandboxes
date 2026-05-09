# Tutorial: Deploy a Welcome Emailer to a Sandbox

End-to-end walkthrough: create a sandbox app that sends welcome emails via the
Office 365 connector using **Direct API calls (Pattern A)**.

## What you'll build

- A connector gateway + OAuth connection to Office 365
- A sandbox running a Python script that sends a welcome email
- Egress transform so the sandbox calls the runtime URL with **no auth code** —
  the platform injects the Bearer token automatically
- Access policy granting the sandbox group MI permission to use the connection

## Prerequisites

- `az` CLI with `aca` extension installed (run `aca --version` to check)
- An Azure subscription with Contributor access
- An Office 365 account (the email sender)

→ Full prerequisites: [prerequisites.md](prerequisites.md)

---

## Step 1: Set up Azure context

```bash
# List subscriptions
az account list --query "[].{name:name, id:id, isDefault:isDefault}" -o table

# Set the subscription you want to use
az account set --subscription "{subscription_id}"

# Create or use an existing resource group
az group create --name welcome-emailer-rg --location eastus
```

Store these for later:
```
SUB="{subscription_id}"
RG="welcome-emailer-rg"
LOCATION="eastus"
```

## Step 2: Create connector gateway

```powershell
$gwBody = @{ location = $LOCATION; identity = @{ type = "SystemAssigned" } } | ConvertTo-Json -Compress
$tmp = New-TemporaryFile; Set-Content $tmp $gwBody
az rest --method PUT `
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Web/connectorGateways/welcome-gw?api-version=2026-05-01-preview" `
  --body "@$tmp"
Remove-Item $tmp
```

Capture the gateway's managed identity (needed for access policies later):
```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Web/connectorGateways/welcome-gw?api-version=2026-05-01-preview" \
  --query "{principalId:identity.principalId, tenantId:identity.tenantId}"
```

Store: `GW_PRINCIPAL_ID` and `GW_TENANT_ID`.

## Step 3: Create connection + consent

Create an OAuth connection to Office 365:

```powershell
$connBody = @{
  location = $LOCATION
  properties = @{ connectorName = "office365" }
} | ConvertTo-Json -Compress
$tmp = New-TemporaryFile; Set-Content $tmp $connBody
az rest --method PUT `
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Web/connectorGateways/welcome-gw/connections/o365-conn?api-version=2026-05-01-preview" `
  --body "@$tmp"
Remove-Item $tmp
```

> **⚠️ Connection body uses `connectorName`** (NOT `api.name`).

Generate the consent link and authenticate:

```bash
az rest --method POST \
  --url "https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Web/connectorGateways/welcome-gw/connections/o365-conn/generateConsentLink?api-version=2026-05-01-preview" \
  --body '{}' \
  --query "consentLink" -o tsv
```

Open the link in a browser, sign in with the Office 365 account, and grant consent.

Verify the connection status:
```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Web/connectorGateways/welcome-gw/connections/o365-conn?api-version=2026-05-01-preview" \
  --query "{status:properties.statuses[0].status, createdBy:properties.createdBy.name}"
```

Status should be `Connected`. The `createdBy.name` field shows who authenticated.

→ Full consent flow details: [consent.md](consent.md)

## Step 4: Create sandbox group + sandbox

```bash
# Create sandbox group
aca sandboxgroup create -g $RG -n welcome-sg -l $LOCATION

# Enable system-assigned managed identity
# (aca sandboxgroup create does NOT support --identity)
aca sandboxgroup update -g $RG -n welcome-sg --identity SystemAssigned

# Capture the sandbox group's principal ID
aca sandboxgroup show -g $RG -n welcome-sg --query "identity.principalId" -o tsv
```

Store: `SG_PRINCIPAL_ID`.

> **⚠️ New sandbox groups take 5–20 minutes to propagate to the data plane.**
> If `create sandbox` fails with `SandboxGroupNotFound`, wait and retry.

```bash
# Create sandbox (retry if SandboxGroupNotFound)
aca sandbox create -g $RG --group welcome-sg --disk ubuntu

# Wait for Running state
aca sandbox show -g $RG --group welcome-sg --id {sandbox_id} --query "state"
```

Store: `SANDBOX_ID`.

Install Python (ubuntu image has none pre-installed):
```bash
aca sandbox exec -g $RG --group welcome-sg --id $SANDBOX_ID \
  -c "apt update && apt install -y python3 python3-pip python3-requests"
```

## Step 5: Configure access policy + egress

Two things are needed so the sandbox can call the Office 365 runtime URL:

### 5a: Access policy (sandbox group MI → connection)

This can be created as soon as the sandbox group MI exists:

```powershell
$aclBody = @{
  location = $LOCATION
  properties = @{
    principal = @{
      type = "ActiveDirectory"
      identity = @{ objectId = "$SG_PRINCIPAL_ID"; tenantId = "$GW_TENANT_ID" }
    }
  }
} | ConvertTo-Json -Depth 5 -Compress
$tmp = New-TemporaryFile; Set-Content $tmp $aclBody
az rest --method PUT `
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Web/connectorGateways/welcome-gw/connections/o365-conn/accessPolicies/sandbox-acl?api-version=2026-05-01-preview" `
  --body "@$tmp"
Remove-Item $tmp
```

### 5b: Get runtime URL

```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Web/connectorGateways/welcome-gw/connections/o365-conn?api-version=2026-05-01-preview" \
  --query "properties.connectionRuntimeUrl" -o tsv
```

Store: `RUNTIME_URL` (looks like `https://...azure-apihub.net/apim/office365/...`).

### 5c: Egress transform (requires sandbox to be Running)

The egress transform injects `Authorization: Bearer {token}` on outbound HTTPS
calls, so the emailer script needs **no auth code at all**.

```powershell
# Extract hostname from runtime URL
$runtimeUrl = "{RUNTIME_URL}"
$hostName = ([System.Uri]$runtimeUrl).Host

$egressBody = @{
  defaultAction = "Allow"
  rules = @(@{
    name = "connection-auth"
    match = @{ host = $hostName }
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
  --url "https://management.azuredevcompute.io/subscriptions/$SUB/resourceGroups/$RG/sandboxGroups/welcome-sg/sandboxes/$SANDBOX_ID/egresspolicy?api-version=2026-02-01-preview" `
  --body "@$tmp" `
  --resource "https://management.azuredevcompute.io/"
Remove-Item $tmp
```

> **⚠️ Critical egress values:**
> - Token resource: `https://management.core.windows.net/` (NOT `management.azure.com`)
> - Format: `"Bearer {value}"` (NOT `{token}`)
> - This is a **REPLACE** operation — overwrites all existing egress rules

→ Full egress details + troubleshooting: [egress-setup.md](egress-setup.md)

## Step 6: Write and deploy the emailer script

Create a local file `welcome_emailer.py`:

```python
#!/usr/bin/env python3
"""Send a welcome email via Office 365 connection runtime URL."""
import os, sys, time

# === SSL setup (MUST be before importing requests) ===
os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"
try:
    import requests
    requests.get("https://management.azure.com", timeout=5)
except Exception:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    import requests
    SSL_VERIFY = False
else:
    SSL_VERIFY = True

# Runtime URL — egress injects auth, do NOT add Authorization header
RUNTIME_URL = os.environ.get("O365_RUNTIME_URL",
    "https://REPLACE_ME.azure-apihub.net/apim/office365/REPLACE_ME")

def send_email(to, subject, body_html, retries=3, timeout=120):
    """Send email via Office 365 connector. Retries on cold-start errors."""
    url = f"{RUNTIME_URL}/v2/Mail"
    # Office 365 SendMailV2 uses a FLAT body — not nested under "emailMessage"
    payload = {
        "To": to,
        "Subject": subject,
        "Body": body_html,
        "Importance": "Normal",
        "IsHtml": True,
    }
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload,
                                 verify=SSL_VERIFY, timeout=timeout)
            if resp.status_code in (200, 202):
                print(f"Email sent to {to}")
                return True
            if resp.status_code in (502, 503, 504) and attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"Cold-start {resp.status_code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            print(f"Failed: {resp.status_code} {resp.text}", file=sys.stdout)
            return False
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                print(f"Timeout, retrying ({attempt+1}/{retries})...")
                time.sleep(5 * (attempt + 1))
                continue
            print("Request timed out after all retries", file=sys.stdout)
            return False

if __name__ == "__main__":
    send_email(
        to="newhire@contoso.com",
        subject="Welcome to the team!",
        body_html="""<h2>Welcome!</h2>
<p>We're excited to have you join us. Here's what to expect on your first day:</p>
<ul>
  <li>9:00 AM — Team standup (link in calendar)</li>
  <li>10:00 AM — IT setup with your manager</li>
  <li>11:30 AM — Lunch with the team</li>
</ul>
<p>See you soon!</p>""",
    )
```

> **⚠️ Do NOT send an `Authorization` header** — the egress transform injects it.
> Adding your own will conflict with the platform-injected token.

> **⚠️ Office 365 `SendMailV2` uses a flat body** (`{"To", "Subject", "Body"}`),
> not nested under `{"emailMessage": {...}}`.

Deploy to the sandbox:
```bash
aca sandbox fs write --id $SANDBOX_ID --path /app/welcome_emailer.py \
  --file ./welcome_emailer.py -g $RG --group welcome-sg
```

→ SSL/retry rationale: [handler-guide.md](handler-guide.md)

## Step 7: Test it

Set the runtime URL environment variable and run:

```bash
aca sandbox exec -g $RG --group welcome-sg --id $SANDBOX_ID \
  -c "O365_RUNTIME_URL='$RUNTIME_URL' python3 /app/welcome_emailer.py"
```

Expected output: `Email sent to newhire@contoso.com`

Check the recipient's inbox to confirm delivery.

**Troubleshooting:**

| Result | Cause | Fix |
|--------|-------|-----|
| `Email sent to ...` ✅ | Working | — |
| `403` | Access policy missing or not propagated | Wait 30s, check ACL exists |
| `401` / "AuthorizationToken required" | Egress rule wrong | Verify resource is `management.core.windows.net` |
| `504` / Timeout | Cold-start latency | Retry — script has built-in backoff |
| `CERTIFICATE_VERIFY_FAILED` | SSL proxy issue | Script handles this automatically (CA bundle → fallback) |

→ Full troubleshooting matrix: [gotchas.md](gotchas.md)

## Step 8: Cleanup

```bash
# Delete in this order: sandbox → group → connection → gateway → resource group
aca sandbox delete -g $RG --group welcome-sg --id $SANDBOX_ID
aca sandboxgroup delete -g $RG -n welcome-sg

# Delete connection + gateway
az rest --method DELETE \
  --url "https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Web/connectorGateways/welcome-gw/connections/o365-conn?api-version=2026-05-01-preview"
az rest --method DELETE \
  --url "https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Web/connectorGateways/welcome-gw?api-version=2026-05-01-preview"

# Optional: delete the resource group entirely
az group delete --name $RG --yes --no-wait
```

## Summary

| Step | What | Key gotcha |
|------|------|-----------|
| 1 | Azure context | Pick subscription + resource group |
| 2 | Gateway | Must have `SystemAssigned` identity |
| 3 | Connection + consent | Use `connectorName` (not `api.name`); verify `createdBy.name` |
| 4 | Sandbox group + sandbox | `update --identity SystemAssigned` after create; install Python |
| 5 | ACL + egress | Token resource = `management.core.windows.net`; egress REPLACES all rules |
| 6 | Emailer script | Flat email body; no auth header; timeout=120 |
| 7 | Test | Check recipient inbox |
| 8 | Cleanup | Delete sandbox → group → connection → gateway |
