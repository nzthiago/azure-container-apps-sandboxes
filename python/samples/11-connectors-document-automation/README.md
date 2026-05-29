# Automate invoice extraction from SharePoint

Drop an invoice PDF into a SharePoint folder. A sandbox wakes up,
reads the file (with OCR when it's scanned), pulls out the vendor,
dates, line items, and totals, and saves the structured data back to
SharePoint as JSON next to the original.

How it works in one paragraph. A Connector Namespaces trigger watches
the SharePoint folder. When a new file shows up, the trigger calls a
sandbox directly over HTTPS, no glue code in between. Inside the
sandbox, GitHub Copilot CLI uses the SharePoint MCP server to
download the file, runs `pdftotext` or `tesseract` to get the text,
builds the JSON, and uploads the result through the same MCP server.

## Deploy and test

You'll need `azd`, the `az` CLI, Python 3.10+, a SharePoint site you
can sign in to, and a GitHub PAT with access to
[GitHub Models](https://github.com/marketplace/models).

```bash
azd auth login
az login

azd env set GITHUB_PAT              <ghp_…>
azd env set SHAREPOINT_SITE_URL     'https://contoso.sharepoint.com/teams/Finance'
azd env set SHAREPOINT_LIBRARY_ID   '<library-list-GUID>'

# Optional. Default: process every new file in the library, write to /Extracted.
azd env set SHAREPOINT_INPUT_FOLDER  'testinvoices/inbound'
azd env set SHAREPOINT_OUTPUT_FOLDER 'testinvoices/extracted'

azd up
```

The post-deploy script does a few things. It boots a sandbox and
installs the toolchain (`poppler-utils`, `tesseract`, Copilot CLI,
and a small FastAPI listener on port 8080). It applies a deny-default
egress policy on the sandbox with two Transform rules: one that
stamps `X-API-Key` on outbound calls to the SharePoint MCP host, and
one that stamps `Authorization` on calls to the GitHub Copilot hosts.
It registers port 8080 on the sandbox proxy, creates the trigger
config, and opens two browser tabs so you can sign in to the two
SharePoint connections (`sharepointonline` for the trigger,
`workiqsharepoint` for the MCP).

To verify, drop one of the [sample invoices](samples/invoices) into
your input folder. Both produce the same JSON:

```json
{
  "vendor": "Contoso Office Supplies, Inc.",
  "invoice_number": "INV-2026-00427",
  "subtotal": 3774.94, "tax": 328.42, "total": 4103.36,
  "line_items": [...]
}
```

| File | Path the agent takes |
|---|---|
| `invoice-text.pdf` | `pdftotext` extracts directly. |
| `invoice-scanned.pdf` | `pdftoppm` rasterizes, then `tesseract` OCRs the page. |

If you tweak the prompt or the listener and want to push the change
without a full re-provision, run
`python infra/scripts/postdeploy.py --skip-oauth`.

## Clean up

While this sample is deployed, every new file in the configured
SharePoint folder wakes the sandbox and Copilot CLI runs against it
(which consumes GitHub Models tokens). Tear it down when you're done:

```bash
azd down --purge --force
```

---

## How it works

```
SharePoint new-file event
    │  (Connector Namespace polls every 10s)
    ▼
triggerConfig (GetOnNewFileItems)
    │  POST callbackUrl
    │  Authorization: Bearer <namespace MI token, aud=https://auth.adcproxy.io/>
    ▼
https://<sandboxId>--8080.<region>.adcproxy.io
    │  Entra check: caller oid ∈ port.entraId.objectIds (= namespace MI)
    │  wake sandbox if cold (activationMode=OnDemand) and forward
    ▼
sandbox /listener (FastAPI :8080)
    │  filter: skip files in output folder, *.json, or outside input folder
    │  spawn: copilot --allow-all-tools -p prompt.md
    │  egress: Deny default; Transform stamps X-API-Key on MCP host
    ▼
SharePoint MCP (workiqsharepoint via Connector Namespace)
    getSiteByPath → listDocumentLibrariesInSite → getFolderChildren
    → readSmallBinaryFile → (shell: pdftotext / tesseract)
    → createSmallTextFile → /<output folder>/<filename>.json
```

## Security model

| Component | Role |
|---|---|
| Connector Namespace MI | Signs every trigger POST to the sandbox proxy. Granted `Container Apps SandboxGroup Data Owner` so the proxy can wake the sandbox on demand. |
| `sharepointonline` connection | OAuth to your SP site. Used only by the trigger. |
| `workiqsharepoint` connection | OAuth to your SP site. Used only by the MCP backend the sandbox calls. |
| mcpserverConfig (`kind: ManagedMcpServer`) | Namespace-published MCP HTTP endpoint. Authenticates with `X-API-Key` (stamped by egress proxy). |
| Sandbox egress proxy | Deny default plus Transform rules that stamp `X-API-Key` on the MCP host and `Authorization` on the GitHub Copilot hosts. |

Three independent checks gate the flow:

1. **Trigger to sandbox.** `adcproxy.io` validates the namespace MI
   bearer token. Wrong `oid` returns 403, no token returns 401.
2. **Sandbox to MCP.** The MCP endpoint requires `X-API-Key`. The
   sandbox never holds the key. The egress proxy stamps it at the
   boundary. The Deny default drops everything else.
3. **MCP to SharePoint.** `workiqsharepoint` calls Microsoft Graph
   with the connection's OAuth token. The token never leaves the
   namespace runtime.

The sandbox holds **no SharePoint credential and no MCP API key**.
The only secret-shaped value inside is the GitHub PAT, which Copilot
CLI needs locally before it can make any network call. See "Going
further" for the path off that trade-off.

## Going further

This scenario uses one long-lived host sandbox that handles requests
through Copilot CLI in sequence. For one fresh sandbox per invoice
(destroyed after the run completes), the listener would call the
sandbox group API to spawn a child sandbox per trigger. That needs
in-sandbox managed identity, which isn't yet in this preview.