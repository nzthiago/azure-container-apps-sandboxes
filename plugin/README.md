# Azure Container Apps Sandboxes — Plugin Marketplace

Skills for Azure Container Apps Sandboxes — secure, isolated, ephemeral compute environments with sub-second startup, snapshot/resume, and scale-to-zero. Manage sandbox groups, sandboxes, exec, SSH, files, ports, egress, images, and snapshots.

## Install

### GitHub Copilot CLI

```bash
# Add as a plugin marketplace
/plugin marketplace add Azure-Samples/azure-container-apps-sandboxes

# Install sandbox skill
/plugin install azure-sandbox@Azure-Container-Apps

# Or install a specific skill
/plugin install azure-sandbox/sandbox@Azure-Container-Apps
```

### Claude Code

```bash
# Add the plugin
claude plugin add Azure-Samples/azure-container-apps-sandboxes
```

## Prerequisites

Skills reference the sandbox az CLI extension and Python SDK. Install them:

```bash
# From GitHub Release (SDK)
gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_sandbox-*-py3-none-any.whl
gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_mgmt_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_mgmt_sandbox-*-py3-none-any.whl

# From GitHub Release (az CLI extension)
gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern "az_cli_sandbox-*-py3-none-any.whl" --dir /tmp
az extension add --source /tmp/az_cli_sandbox-*-py3-none-any.whl
```

For durable orchestration scenarios, start with the Durable Task Workflows lab in [`../labs/02-durable-task-workflows`](../labs/02-durable-task-workflows). It stays sample-only: install the sandbox wheels from this repo's GitHub Releases, then add the official `az durabletask` extension separately for scheduler and task hub lifecycle.

## Uninstall

```bash
az extension remove --name sandbox
pip uninstall azure-sandbox azure-mgmt-sandbox
```

## Skills

| Skill | Domain | Status |
|-------|--------|--------|
| [azure-sandbox](skills/azure-sandbox/SKILL.md) | Sandbox groups, sandboxes, exec, SSH, files, ports, egress, images, snapshots | Available |
