# 10-connectors-triggers — Push outside events into a sandbox

A feedback-analyzer demo: when an email with **Feedback** in the subject
lands in your Office 365 inbox, a sandbox automatically reads it, asks
the **GitHub Copilot CLI** to draft a personalized acknowledgment, and
replies — all without you lifting a finger.

The plumbing: the **Office 365 connector** detects the new email and
fires an InvokePort trigger callback that POSTs the email to a small
HTTP listener running inside a sandbox. The port is created with
`activationMode=OnDemand`, so the proxy automatically RESUMES the
sandbox if it has scaled to zero. The Copilot CLI (pre-installed on the
`copilot` disk image) composes the reply, and the sandbox sends it back
through the **same** Office 365 connection — one OAuth consent, both
directions. The sandbox can idle / suspend between emails; the platform
wakes it back up for each delivery.

The same shape applies to every trigger the connector platform exposes
(SharePoint, OneDrive, Teams, Dataverse, third-party SaaS, on-prem APIs).

## Prerequisites

- [Azure Developer CLI](https://aka.ms/azd) (`azd`)
- [Azure CLI](https://aka.ms/azcli) (`az`), logged in via `az login`
- Python 3.10+
- An Office 365 mailbox you can OAuth-consent into
- A GitHub token usable by the Copilot CLI (see
  [`feedback-analyzer/`](feedback-analyzer) for accepted token types)
- Optional: an existing sandbox group with the
  **Container Apps SandboxGroup Data Owner** role assigned to your
  principal — set its name as `ACA_SANDBOX_GROUP` in the repo-root
  `.env` (or `azd env set ACA_SANDBOX_GROUP <name>`). If unset,
  `azd up` creates a group named `ai-apps-samples-group` in the
  scenario resource group and assigns that role to you automatically.
  See
  [`cli/samples/00-get-started/00-sandbox-groups/`](../00-get-started/00-sandbox-groups/)
  for one way to provision your own.

## Quickstart

```bash
cd cli/samples/10-connectors-triggers
azd auth login          # if you haven't already
az login                # the postprovision hook also needs the az CLI
azd up
```

> [!IMPORTANT]
> **Pick a sandbox-supported region.** `Microsoft.App/sandboxGroups` is
> only available in a fixed set of regions (`westus2`, `eastus2`,
> `westus3`, `centralus`, `northeurope`, `uksouth`, …). The default is
> `westus2`. To pick a different one:
>
> ```bash
> azd env set AZURE_LOCATION westus2
> azd up
> ```
>
> If you already provisioned into an unsupported region, run
> `azd down --purge` first, then re-run `azd up`.

After `azd up` completes (you'll click an OAuth consent link once for the
Office 365 connection), fire the demo:

```bash
# Linux / macOS
bash feedback-analyzer/run.sh
```

```bat
:: Windows (cmd or PowerShell) — uses Git Bash automatically
feedback-analyzer\run.cmd
```

> [!NOTE]
> On Windows, `bash feedback-analyzer/run.sh` may resolve to the WSL `bash`
> shim, which can't share temp files with the Windows-native `az` CLI the
> script uses. The `.cmd` wrapper finds Git Bash explicitly and avoids that.

Tear everything down with `azd down`. The `predown` hook calls
`setup/teardown.sh` to delete the connector gateway (cascading the
office365 connection and trigger configs) and to detach the entry
from the sandbox group's `gatewayConnections[]`, before azd deletes
the resource group.

## Sub-scenarios

| Folder | What it shows |
|---|---|
| [`setup/`](setup) | One-time provisioning: a connector + Office 365 connection (with OAuth consent) + access policies. |
| [`feedback-analyzer/`](feedback-analyzer) | The end-to-end demo. Real Office 365 inbox → trigger → sandbox listener (auto-resumed on demand) → Copilot CLI reply → SendMailV2. |

The Python flavor of this sample lives at [`python/samples/10-connectors-triggers/`](../../../python/samples/10-connectors-triggers/).