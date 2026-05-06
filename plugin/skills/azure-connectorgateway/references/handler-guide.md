# Handler Development Guide

How to build and deploy handler scripts to sandboxes.

## CRITICAL: Collect ALL handler parameters BEFORE writing code

> **Do NOT hardcode folder paths, channel IDs, site URLs, list names, or any
> connector-specific values.** Fetch them via `dynamicInvoke` and let the user choose.

| Handler needs... | How to collect |
|-----------------|----------------|
| OneDrive folder path | `dynamicInvoke` GET `/datasets/default/folders` → present choices |
| SharePoint site | `dynamicInvoke` GET `/datasets` → present choices |
| SharePoint list | `dynamicInvoke` GET `/datasets/{site}/tables` → present choices |
| Teams team/channel | `dynamicInvoke` GET `/beta/me/joinedTeams` → present choices |
| Email folder | Default `Inbox` (inform user), or fetch via `/datasets/default/folders` |

→ Full algorithms: See [dynamic-values.md](dynamic-values.md)

## How event data reaches the handler

| Target type | Event data delivery | Handler approach |
|-------------|-------------------|-----------------|
| **InvokePort** (`--port --port-path`) | ✅ In POST body | Parse `request.json` (Flask) |
| **ShellCommand** (`--command`) | ❌ NOT passed | Handler must fetch via runtime URL |
| **ExecuteCommand** (`--execute-command`) | ❌ NOT passed | Same as ShellCommand |

**How to determine your target type:**
- `--port` + `--port-path` → InvokePort (event data in POST body)
- `--command` → ShellCommand (must fetch data yourself)
- Check `callbackUrl`: `proxy.azuredevcompute.io` → InvokePort; `executeShellCommand` → ShellCommand

## Sandbox environment details

| Feature | Details |
|---------|---------|
| **Managed Identity** | App Service-style (NOT IMDS). Use `IDENTITY_ENDPOINT` + `IDENTITY_HEADER` |
| **Python HTTP library** | Use `requests` or `urllib`. `httpx` has SSL issues |
| **stdin** | Empty — cannot pass data via stdin |
| **Environment variables** | Work via `executeShellCommand`'s `environment` field |
| **Auth for runtime URL calls** | NOT needed — egress transform injects Bearer automatically |
| **File system** | Writable at `/app/`. Deploy handler scripts here |
| **SSL/TLS** | Prefer `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`. Fallback: `verify=False` + suppress warnings |
| **stderr = failure** | Trigger runtime treats ANY stderr output as failure. Suppress all warnings. |

## Critical: SSL and stderr handling

> **⚠️ The trigger runtime marks a ShellCommand as FAILED if anything is written to stderr,
> even if exit code is 0.** This means `InsecureRequestWarning` from `verify=False` will
> cause silent trigger failures.

**Correct approach (in order of preference):**

```python
import os

# Option 1 (PREFERRED): Use system CA bundle — no warnings, no issues
os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"
import requests
# All requests now use the sandbox proxy CA — no verify=False needed

# Option 2 (FALLBACK): If CA bundle fails, disable verification + suppress warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import requests
# Now use verify=False without stderr output
```

**Every handler MUST include one of these at the top, before any `requests` calls.**

## MI token in sandbox (App Service-style)

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
> **You usually don't need MI tokens.** Egress transform handles auth on runtime URL calls.
> MI is only for calling other Azure services directly.

## O365 connector quirks

| Quirk | Workaround |
|-------|-----------|
| `HasAttachment` is singular | Use `HasAttachment` not `HasAttachments` |
| `hasAttachments=true` filter unreliable | Fetch top N, filter client-side |
| Attachments: use `includeAttachments=true` | Add `?includeAttachments=true` to `/v2/Mail` query — returns `Attachments[]` with `ContentBytes` (base64) inline |
| `/codeless/` or `/v1.0/` attachment endpoints | ❌ Return 404 from runtime URLs. Do NOT use separate attachment endpoints. |
| `contentBytes: null` without flag | Always pass `includeAttachments=true` |
| Inline images count as attachments | Filter with `not att.get("IsInline", False)` |
| `includeAttachments=true` intermittent | Add retry (3 attempts, 2s delay) |

## Deploying handler to sandbox

**Primary method — `aca sandbox fs write` (recommended):**
```bash
# Write handler code to a local file first, then upload to sandbox
# (avoids all shell escaping issues with Python f-strings and curly braces)
aca sandbox fs write --id {sandbox_id} --path /app/handler.py --file ./handler.py -g {rg} --group {sandbox_group}
```

**Alternative — via exec (for small scripts only):**
```bash
aca sandbox exec --id {sandbox_id} -c "cat > /app/handler.py << 'HANDLER_EOF'
<paste script here>
HANDLER_EOF" -g {rg} --group {sandbox_group}
```

> **⚠️ Do NOT try to pass Python code as an inline PowerShell string.**
> Python f-strings, curly braces, and nested quotes break PowerShell parsing.
> Always write the handler to a local temp file first, then upload with `aca sandbox fs write`.

## Handler template (ShellCommand + runtime URL)

```python
#!/usr/bin/env python3
"""Handler for ShellCommand triggers calling connection runtime URLs."""
import os, sys, time, json

# === SSL SETUP (MUST be before any requests import) ===
os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"
try:
    import requests
    # Test that CA bundle works
    requests.get("https://management.azure.com", timeout=5)
except Exception:
    # Fallback: disable SSL verification + suppress warnings
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    import requests
    SSL_VERIFY = False
else:
    SSL_VERIFY = True

# Runtime URLs — egress handles auth, NO Bearer token needed
O365_URL = os.environ.get("O365_RUNTIME_URL", "https://....azure-apihub.net/apim/office365/...")
ONEDRIVE_URL = os.environ.get("ONEDRIVE_RUNTIME_URL", "https://....azure-apihub.net/apim/onedriveforbusiness/...")

def http_get(url, retries=3, delay=2):
    """GET with retry — connector API can be intermittent."""
    for attempt in range(retries):
        resp = requests.get(url, verify=SSL_VERIFY)
        if resp.status_code == 200:
            return resp.json()
        if attempt < retries - 1:
            time.sleep(delay)
    return None

def http_post(url, data=None, json_body=None, content_type="application/json"):
    """POST to runtime URL."""
    headers = {"Content-Type": content_type}
    if json_body:
        return requests.post(url, json=json_body, headers=headers, verify=SSL_VERIFY)
    return requests.post(url, data=data, headers=headers, verify=SSL_VERIFY)

def main():
    # 1. Fetch data from source connector
    # 2. Process / transform
    # 3. Write to destination connector
    pass

if __name__ == "__main__":
    main()
```

**Key points:**
- SSL: CA bundle first, `verify=False` + `disable_warnings()` as fallback
- **Never leave `verify=False` without `urllib3.disable_warnings()`** — stderr = trigger failure
- Add retry logic (2-3 attempts, 2s delay)
- Egress handles auth — do NOT add Authorization headers
- Use `requests` not `httpx`
- Deploy via `aca sandbox fs write` (preferred) or base64 pipe
- For email attachments: use `includeAttachments=true` on `/v2/Mail`, NOT separate endpoints
