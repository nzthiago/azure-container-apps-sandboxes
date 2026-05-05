# Sandbox Getting Started

Your first sandbox — from zero to running commands in a hardware-isolated microVM.

## What You'll Do

1. Create a sandbox group (your container for sandboxes)
2. Spin up a sandbox from a disk image
3. Run commands inside the sandbox
4. Expose a port for network access
5. Take a snapshot (save your work)
6. Suspend and resume (pick up exactly where you left off)
7. Clean up

## How to Run

| Mode | How | Best for |
|------|-----|----------|
| **Notebook** | Open `01-getting-started.ipynb` in VS Code | Humans — step by step with explanations |
| **Script** | See [getting-started-runbook.md](../../plugin/skills/azure-sandbox/references/getting-started-runbook.md) | Agents, CI, quick test |

## Prerequisites

- Azure CLI: `az login`
- SDK: download from [GitHub Release](https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases) and `pip install azure_containerapps_sandbox-*-py3-none-any.whl`
