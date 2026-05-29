# ACA Sandbox: SharePoint document automation

> **A SharePoint file-created event POSTs directly to an ACA Sandbox
> HTTPS endpoint — no receiver app, no Function host. Inside the
> sandbox, GitHub Copilot CLI uses the SharePoint MCP to fetch the
> file, `pdftotext` / `tesseract` extract the invoice, and the result
> is written back to SharePoint via the same MCP.**

## Deploy and test

**Prereqs:** `azd`, `az` CLI, Python 3.10+, a SharePoint site you
can authorize against, and a GitHub PAT with access to [GitHub
Models](https://github.com/marketplace/models).

```bash
azd auth login
az login

azd env set GITHUB_PAT              <ghp_…>
azd env set SHAREPOINT_SITE_URL     'https://contoso.sharepoint.com/teams/Finance'
azd env set SHAREPOINT_LIBRARY_ID   '<library-list-GUID>'

# Optional: scope to a subfolder (default: process every new file in the library,
# write to /Extracted)
azd env set SHAREPOINT_INPUT_FOLDER  'testinvoices/inbound'
azd env set SHAREPOINT_OUTPUT_FOLDER 'testinvoices/extracted'

azd up
```

The post-deploy hook bootstraps the sandbox (`poppler-utils`,
`tesseract`, Copilot CLI, FastAPI listener on `:8080`), applies a
deny-default egress policy with `X-API-Key` + `Authorization`
Transform rules, registers the sandbox port on the ADC proxy, creates
the trigger config, and opens **two** browser tabs for OAuth consent
(`sharepointonline` for the trigger, `workiqsharepoint` for the MCP).

**Confirm end-to-end** — drop one of the [sample invoices](samples/invoices)
into your input folder. Both produce the same JSON:

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
| `invoice-text.pdf` | `pdftotext` extracts directly |
| `invoice-scanned.pdf` | `pdftoppm` + `tesseract` OCR fallback |

For prompt/listener tweaks without a full re-provision:
`python infra/scripts/postdeploy.py --skip-oauth`.

## Clean up

⚠️ While deployed, every new file in the configured SharePoint
folder wakes the sandbox and Copilot CLI runs against it
(consuming GitHub Models tokens). Tear down when you're done:

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
| `sharepointonline` connection | OAuth → your SP site. Used only by the trigger. |
| `workiqsharepoint` connection | OAuth → your SP site. Used only by the MCP backend the sandbox calls. |
| mcpserverConfig (`kind: ManagedMcpServer`) | Namespace-published MCP HTTP endpoint. Authenticates with `X-API-Key` (stamped by egress proxy). |
| Sandbox egress proxy | Deny default + Transform rules that stamp `X-API-Key` on MCP host and `Authorization` on the GitHub Copilot hosts. |

Three independent checks gate the flow:

1. **Trigger → sandbox** — `adcproxy.io` validates the namespace MI
   bearer token; wrong `oid` → 403, no token → 401.
2. **Sandbox → MCP** — MCP endpoint requires `X-API-Key`; the
   sandbox never has the key, the egress proxy stamps it at the
   boundary. Deny default drops everything else.
3. **MCP → SharePoint** — `workiqsharepoint` calls Microsoft Graph
   with the connection's OAuth token. Token never leaves the
   namespace runtime.

The sandbox holds **no SharePoint credential and no MCP API key**.
The only secret-shaped thing inside is the GitHub PAT (Copilot CLI
needs it locally before any network call — see "Going further" for
the path off that trade-off).

## Going further

Today this scenario uses **one long-lived host sandbox** that
serializes requests through Copilot CLI. For per-file isolation
(one fresh ACA Sandbox per invoice, destroyed after), the listener
would spawn a child sandbox per trigger via the sandbox group
API — pending in-sandbox managed identity support that's not yet
in this preview.
