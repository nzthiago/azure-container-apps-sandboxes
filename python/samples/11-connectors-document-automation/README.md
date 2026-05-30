# Automate invoice extraction from SharePoint

 A finance team gets invoices uploaded to a SharePoint folder all day, and someone has to read each one and type the vendor, dates, line items, and totals into another system. This sample automates the extraction half. Drop a PDF into the folder, a sandbox wakes up, reads the file (with OCR when it's scanned), and saves the structured data back to SharePoint as JSON next to the original. The JSON output is a clean hand-off point for your finance system.

The way it works is a Connector Namespaces trigger watches a SharePoint folder that you configure. When a new file shows up, the trigger calls an existing sandbox directly over HTTPS. Inside the sandbox, GitHub Copilot CLI uses the SharePoint MCP server to download the file, runs tool like `pdftotext` or `tesseract` to get the text, builds a JSON summary of the invoice, and uploads the result through the same MCP server.

 The sandbox has apt packages and Copilot CLI installed, and is Stopped (zero compute charge) between events, with a transform rule that means the sandbox process never holds the MCP key. This way, you have a programmable Linux box that wakes on an HTTPS event, with a credential-mediating boundary.


## Deploy and test

You'll need [Azure Developer CLI](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd?tabs=winget-windows%2Cbrew-mac%2Cscript-linux&pivots=os-windows), [the az CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli?view=azure-cli-latest), the [GitHub CLI](https://cli.github.com/), Python 3.10+, a SharePoint site you can sign in to, and a GitHub account with access to [GitHub Models](https://github.com/marketplace/models).

### First Run

This will create a new GitHub PAT token and add to the AZD environment, and allow the post-deploy scripts to be executed. These are only needed when starting from scratch.

For Mac/Linux:
```bash
azd auth login
chmod +x infra/scripts/postdeploy.sh
gh auth refresh -h github.com -s read:user
 azd env set GITHUB_PAT $(gh auth token)
```
For Windows/PowerShell:

```powershell
azd auth login
set-executionpolicy remotesigned
gh auth refresh -h github.com -s read:user
azd env set GITHUB_PAT (gh auth token)
```

### Configure and run

For the SharePoint location configuration we need two values, both findable from your browser without any extra tooling:

You can get the **SharePoint site URL** by opening the SharePoint site in any browser. The URL bar shows something like:

 https://contoso.sharepoint.com/teams/Finance/Shared Documents/Forms/AllItems.aspx

The site URL is everything up to and including /teams/Finance (or /sites/<name> for non-Teams sites). So:

 SHAREPOINT_SITE_URL = https://contoso.sharepoint.com/teams/Finance

Trim trailing slashes. 

For **SharePoint library ID** this is the list GUID for the document library. In the browser: 
 1. Open the document library you want (commonly "Documents" / "Shared Documents", or a custom library you created).
 2. Click the gear ⚙ icon (top right) → Library settings → More library settings.
 3. Look at the URL bar. It contains ?List=%7B<GUID>%7D. The part between the encoded %7B (which is {) and %7D (which is }) is your library ID.

Example URL:

 https://contoso.sharepoint.com/teams/Finance/_layouts/15/listedit.aspx?List=%7BE01BA0E8-6CE5-4B05-A9B7-B4C49BBC6259%7D

You can also run the test with input and output subfolders (like /testinvoices/inbound and testinvoites/extracted).

The set it up as following including `azd up` to trigger the full deployment:

```bash
azd env set SHAREPOINT_SITE_URL     'https://contoso.sharepoint.com/teams/Finance'
azd env set SHAREPOINT_LIBRARY_ID   '<library-list-GUID>'

# Optional. Default: process every new file in the library, write to /Extracted.
azd env set SHAREPOINT_INPUT_FOLDER  'testinvoices/inbound'
azd env set SHAREPOINT_OUTPUT_FOLDER 'testinvoices/extracted'

azd up
```

The post-deploy script does a few things. It boots a sandbox and installs the toolchain (`poppler-utils`, `tesseract`, Copilot CLI, and a small FastAPI listener on port 8080). It applies a deny-default egress policy on the sandbox with two Transform rules: one that stamps `X-API-Key` on outbound calls to the SharePoint MCP host, and one that stamps `Authorization` on calls to the GitHub Copilot hosts. It registers port 8080 on the sandbox proxy, creates the trigger config, and opens two browser tabs so you can sign in to the two SharePoint connections (`sharepointonline` for the trigger, `workiqsharepoint` for the MCP).

To verify, drop one of the [sample invoices](samples/invoices) into your input folder on SharePoint. Both should produce a new JSON file on the SharePoint folder:

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

If you tweak the prompt or the listener and want to push the change without a full re-provision, run `python infra/scripts/postdeploy.py --skip-oauth`.

## Clean up

While this sample is deployed, every new file in the configured SharePoint folder wakes the sandbox and Copilot CLI runs against it (which consumes GitHub Models tokens). Tear it down when you're done:

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
## Going further

This scenario uses one long-lived host sandbox that handles requests through Copilot CLI in sequence. For one fresh sandbox per invoice (destroyed after the run completes), the listener would call the sandbox group API to spawn a child sandbox per trigger. That needs in-sandbox managed identity, which isn't yet in this preview but is on the roadmap.