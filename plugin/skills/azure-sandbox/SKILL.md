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
  "deploy to sandbox", "exec", "snapshot", "suspend", "resume", "ssh"
---

# Azure Container Apps Sandboxes

A first-class resource type in Azure Container Apps providing fast, secure, ephemeral compute environments with built-in suspend and resume. Sandboxes offer sub-second startup from prewarmed pools, strong per-sandbox isolation for untrusted code, scale-to-zero billing, massive scale-out to thousands of concurrent instances, OCI container image support, and full-state snapshots that capture memory and disk for instant resume.

## Install

### As plugin (coding agents)
```bash
# Copilot CLI
/plugin marketplace add Azure-Samples/azure-container-apps-sandboxes
/plugin install azure-sandbox@Azure-Container-Apps

# Claude Code
claude plugin add Azure-Samples/azure-container-apps-sandboxes
```

### az CLI extension
```bash
# From GitHub Release
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "az_cli_sandbox-*-py3-none-any.whl" --dir /tmp
az extension add --source /tmp/az_cli_sandbox-*-py3-none-any.whl

# From cloned repo
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "az_cli_sandbox-*-py3-none-any.whl" --dir /tmp
az extension add --source /tmp/az_cli_sandbox-*-py3-none-any.whl
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
az extension remove --name sandbox    # remove az CLI extension
pip uninstall azure-sandbox azure-mgmt-sandbox             # remove standalone SDK (if installed separately)
```

> If a user asks to uninstall or remove the sandbox extension, run `az extension remove --name sandbox`.

## az CLI

Run `az sandboxgroup --help` and `az sandbox --help` to see all available commands.

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

## SSH

See [ssh-setup.md](references/ssh-setup.md). On Windows, prefer the Node.js option for best experience.

## Labs

See [labs/README.md](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/README.md) for all labs.

## References

- [prerequisites.md](references/prerequisites.md)
- [quickstart.md](references/quickstart.md)
- [deploy-patterns.md](references/deploy-patterns.md)
- [ssh-setup.md](references/ssh-setup.md)



