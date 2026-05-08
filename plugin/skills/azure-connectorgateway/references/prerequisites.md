# Prerequisites

## Required

| Requirement | Check | Install |
|-------------|-------|---------|
| Azure CLI | `az --version` | [Install](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azure login | `az account show` | `az login` |
| Node.js 18+ | `node --version` | [nodejs.org](https://nodejs.org) |
| ACA CLI | `aca --version` | `gh release download v0.1.0b1 --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure-containerapps-cli-*.tgz" --dir /tmp && npm install -g /tmp/azure-containerapps-cli-*.tgz` |
| Python 3.10+ | `python --version` | [python.org](https://python.org) |

> **⚠️ There are NO `az` commands for sandboxes.** Gateway = `az rest`. Sandbox = `aca` CLI.
> Do NOT use `az sandbox`, `az sandboxgroup`, or `az connectorgateway`.
> The Python SDK (`SandboxClient`) is not shipped with the current CLI release — use `aca` CLI + `az rest` instead.

## Azure Setup (one-time)

```bash
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.Web
az group create --name my-rg --location eastus2
```
