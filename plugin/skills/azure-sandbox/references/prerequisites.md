# Prerequisites

## Required

| Requirement | Check | Install |
|-------------|-------|---------|
| Azure CLI | `az --version` | [Install](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azure login | `az account show` | `az login` |
| Python 3.10+ | `python --version` | [python.org](https://python.org) |

## Install SDK + CLI Extension

```bash
# From GitHub Release
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "az_cli_sandbox-*-py3-none-any.whl" --dir /tmp
az extension add --source /tmp/az_cli_sandbox-*-py3-none-any.whl

# Or from cloned repo
git clone https://github.com/Azure-Samples/azure-container-apps-sandboxes.git
cd azure-container-apps-sandboxes
gh release download --pattern "az_cli_sandbox-*-py3-none-any.whl" --dir /tmp
az extension add --source /tmp/az_cli_sandbox-*-py3-none-any.whl
```

## Azure Setup (one-time)

```bash
az provider register --namespace Microsoft.App
az group create --name my-rg --location westus2
```
