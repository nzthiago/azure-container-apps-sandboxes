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
| **SSL/TLS** | Use `verify=False` or system CA store. Proxy CA may intercept |

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
| `includeAttachments=true` intermittent | Add retry (3 attempts, 2s delay) |
| `/v2/Mail/{id}/Attachments/{id}` returns 404 | Use `/codeless/v1.0/me/messages/{id}/attachments/{id}` |
| `contentBytes: null` without flag | Always pass `includeAttachments=true` |
| Inline images count as attachments | Filter with `not att.get("IsInline", False)` |

## Handler template (ShellCommand + runtime URL)

```python
#!/usr/bin/env python3
"""Handler for ShellCommand triggers calling connection runtime URLs."""
import os, time, json, requests

# Runtime URLs — egress handles auth, NO Bearer token needed
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

**Key points:**
- Use `verify=False` — sandbox may lack CA certs
- Add retry logic (2-3 attempts, 2s delay)
- Egress handles auth — do NOT add Authorization headers
- Use `requests` not `httpx`
- Deploy via `executeShellCommand`: `echo '<base64>' | base64 -d > /app/handler.py`
