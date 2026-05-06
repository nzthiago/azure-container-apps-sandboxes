# Prerequisites

## Required

| Requirement | Check | Install |
|-------------|-------|---------|
| Azure CLI | `az --version` | [Install](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azure login | `az account show` | `az login` |
| Node.js 18+ | `node --version` | [nodejs.org](https://nodejs.org) |
| ACA CLI | `aca --version` | `gh release download v0.1.0b1 --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure-containerapps-cli-*.tgz" --dir /tmp && npm install -g /tmp/azure-containerapps-cli-*.tgz` |
| Python 3.10+ | `python --version` | [python.org](https://python.org) |

> **⚠️ There are NO `az` commands for sandboxes.** Gateway = `az rest`. Sandbox = `aca` CLI (preferred) or Python SDK fallback.
> Do NOT use `az sandbox`, `az sandboxgroup`, or `az connectorgateway`.

## SDK fallback (if aca CLI install fails with 404)

```powershell
# Check if sandbox SDK is already installed:
pip show sandbox-sdk 2>$null
python -c "from sandbox import SandboxClient; print('SDK available')" 2>$null
# If SDK found: use SandboxClient for sandbox ops (write_file, exec, etc.)
# Import: try `from sandbox import SandboxClient` first, then `from azure.containerapps.sandbox import SandboxClient`
```

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
