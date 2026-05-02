---
name: azure-sandbox
description: |
  Manage Azure Container Apps Sandboxes — secure, isolated, ephemeral compute
  environments with sub-second startup, snapshot/resume, scale-to-zero, and
  OCI container image support. Use when:
  - Creating or managing sandbox groups (Microsoft.App/SandboxGroups)
  - Creating sandboxes, executing commands, managing ports
  - Stopping, resuming, snapshotting sandboxes
  - Deploying apps or MCP servers to sandboxes
  Triggers: "create sandbox", "sandbox group", "run command in sandbox",
  "deploy to sandbox", "exec", "snapshot", "suspend", "resume", "shell"
---

# Azure Container Apps Sandboxes

A first-class resource type in Azure Container Apps providing fast, secure, ephemeral compute environments with built-in suspend and resume. Sandboxes offer sub-second startup from prewarmed pools, strong per-sandbox isolation for untrusted code, scale-to-zero billing, massive scale-out to thousands of concurrent instances, OCI container image support, and full-state snapshots that capture memory and disk for instant resume.

## Install

### As plugin (coding agents)
```bash
# Copilot CLI
/plugin marketplace add Azure-Samples/azure-container-apps-sandboxes
/plugin install azure-sandbox@azure-container-apps

# Claude Code
claude plugin add Azure-Samples/azure-container-apps-sandboxes
```

### ACA CLI
```bash
npm install -g https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases/download/v0.1.0b1/azure-aca-cli-1.0.0-beta.1.tgz
```

### Python SDK only
```bash
# From GitHub Release
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_sandbox-*-py3-none-any.whl

# Or install both SDKs
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_mgmt_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_mgmt_sandbox-*-py3-none-any.whl
```

### Uninstall
```bash
npm uninstall -g @azure/aca-cli       # remove ACA CLI
pip uninstall azure-sandbox azure-mgmt-sandbox             # remove standalone SDK (if installed separately)
```

> If a user asks to uninstall or remove the ACA CLI, run `npm uninstall -g @azure/aca-cli`.

## Prerequisites Check

Before running any sandbox commands, check that all required tools are installed and the user is logged in. Run these checks silently and only prompt the user if something is missing.

1. **Azure CLI** — run `az --version`. If missing, tell the user to install it from https://learn.microsoft.com/cli/azure/install-azure-cli
2. **Azure login** — run `az account show`. If it fails, tell the user to run `az login` first.
3. **Node.js 18+** — run `node --version`. If missing, tell the user to install it from https://nodejs.org
4. **ACA CLI** — run `aca --version`. If missing, ask the user if they want to install it:
   ```bash
   npm install -g https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases/download/v0.1.0b1/azure-aca-cli-1.0.0-beta.1.tgz
   ```

## Important

> **There are NO `az` commands for sandboxes or sandbox groups.** Do not use `az sandbox`, `az sandboxgroup`, or `az containerapp` for sandbox operations — these do not exist. `az containerapp` is for Container Apps (apps, jobs, and dynamic sessions), not sandboxes. All sandbox and sandbox group operations use the `aca` CLI. The only `az` commands used are standard Azure CLI commands like `az login`, `az account show`, and `az group create/delete` for resource group management.

## ACA CLI

Run `aca sandboxgroup --help` and `aca sandbox --help` to see all available commands. The ACA CLI requires `az login` for authentication.

## Python SDK

```python
from azure.sandbox import SandboxClient
from azure.mgmt.sandbox import SandboxGroupManagementClient

client = SandboxClient(resource_group="my-rg")
mgmt = SandboxGroupManagementClient(resource_group="my-rg")
```

Run `help(client)` and `help(mgmt)` to see all available methods.

## Portal

- [Sandbox Groups](https://containerapps.azure.com/sandbox-groups)
- [Create Sandbox Group](https://containerapps.azure.com/sandbox-groups/create)
- Sandbox Group detail: `https://containerapps.azure.com/sandbox-groups/<rg>/<name>`
- Sandboxes: `https://containerapps.azure.com/sandbox-groups/<rg>/<name>/sandboxes`
- Sandbox detail: `https://containerapps.azure.com/sandbox-groups/<rg>/<name>/sandboxes/<id>`

## Interactive Shell

```bash
aca sandbox shell --id <sandbox-id> -g <rg> --group <sandbox-group>
```

See [shell-setup.md](references/shell-setup.md) for details.

## Labs

See [labs/README.md](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/README.md) for all labs.

## References

- [prerequisites.md](references/prerequisites.md)
- [quickstart.md](references/quickstart.md)
- [shell-setup.md](references/shell-setup.md)

## Runbooks

- [Getting Started](references/getting-started-runbook.md) — full sandbox lifecycle
- [Deploy Web App](references/deploy-web-app-runbook.md) — upload code, start server, expose port
- [Copilot CLI BYOK](references/copilot-cli-byok-runbook.md) — Azure OpenAI BYOK with zero-trust egress



