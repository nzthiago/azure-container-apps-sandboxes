# Prerequisites

## Required

| Requirement | Check | Install |
|-------------|-------|---------|
| Azure CLI | `az --version` | [Install](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azure login | `az account show` | `az login` |
| Node.js 18+ | `node --version` | [nodejs.org](https://nodejs.org) |
| Python 3.10+ | `python --version` | [python.org](https://python.org) |

## Install ACA CLI + SDK

```bash
# ACA CLI
npm install -g https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases/download/v0.1.0b1/azure-containerapps-cli-1.0.0-beta.1.tgz

# From GitHub Release (SDK)
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_containerapps_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_containerapps_sandbox-*-py3-none-any.whl
```

## Azure Setup (one-time)

```bash
az provider register --namespace Microsoft.App
az group create --name my-rg --location westus2
```
