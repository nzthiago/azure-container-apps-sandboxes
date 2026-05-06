# Prerequisites

## Required

| Requirement | Check | Install |
|-------------|-------|---------|
| Azure CLI | `az --version` | [Install](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azure login | `az account show` | `az login` |
| Node.js 18+ | `node --version` | [nodejs.org](https://nodejs.org) |
| ACA CLI | `aca --version` | `gh release download v0.1.0b1 --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure-containerapps-cli-*.tgz" --dir /tmp && npm install -g /tmp/azure-containerapps-cli-*.tgz` |
| Python 3.10+ | `python --version` | [python.org](https://python.org) |

> **No `az` extensions needed.** All connector gateway operations use `az rest` with ARM APIs directly.
> Sandbox operations use the `aca` CLI. Do NOT use `az sandbox` or `az connectorgateway` — these are not required.

## Install Sandbox SDK (for egress setup)

```bash
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_containerapps_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_containerapps_sandbox-*-py3-none-any.whl
```

## Azure Setup (one-time)

```bash
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.Web
az group create --name my-rg --location eastus2
```
