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
npm install -g @azure/aca-cli

# From GitHub Release (SDK)
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_sandbox-*-py3-none-any.whl
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_mgmt_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_mgmt_sandbox-*-py3-none-any.whl
```

## Azure Setup (one-time)

```bash
az provider register --namespace Microsoft.App
az group create --name my-rg --location westus2
```
