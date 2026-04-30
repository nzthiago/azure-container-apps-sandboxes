# Prerequisites

## Required

| Requirement | Check | Install |
|-------------|-------|---------|
| Azure CLI | `az --version` | [Install](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azure login | `az account show` | `az login` |
| Python 3.10+ | `python --version` | [python.org](https://python.org) |

## Install SDKs

```bash
# From GitHub Release
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_trigger-*.whl" --dir /tmp
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_connector-*.whl" --dir /tmp
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_sandbox-*.whl" --dir /tmp
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_mgmt_sandbox-*.whl" --dir /tmp
pip install /tmp/azure_trigger-*.whl /tmp/azure_connector-*.whl /tmp/azure_sandbox-*.whl /tmp/azure_mgmt_sandbox-*.whl
```

## Azure Setup (one-time)

```bash
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.Web
az group create --name my-rg --location eastus2
```
