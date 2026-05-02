# Azure Container Apps Sandboxes — Plugin Marketplace

Skills for Azure Container Apps Sandboxes — secure, isolated, ephemeral compute environments with sub-second startup, snapshot/resume, and scale-to-zero. Manage sandbox groups, sandboxes, exec, shell, files, ports, egress, images, and snapshots.

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

Skills reference the ACA CLI and Python SDK. Install them:

```bash
# ACA CLI
npm install -g https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases/download/v0.1.0b1/azure-aca-cli-1.0.0-beta.1.tgz

# From GitHub Release (SDK)
gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_sandbox-*-py3-none-any.whl
gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_mgmt_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_mgmt_sandbox-*-py3-none-any.whl
```

## Uninstall

```bash
npm uninstall -g @azure/aca-cli
pip uninstall azure-sandbox azure-mgmt-sandbox
```

## Skills

| Skill | Domain | Status |
|-------|--------|--------|
| [azure-sandbox](skills/azure-sandbox/SKILL.md) | Sandbox groups, sandboxes, exec, shell, files, ports, egress, images, snapshots | Available |
