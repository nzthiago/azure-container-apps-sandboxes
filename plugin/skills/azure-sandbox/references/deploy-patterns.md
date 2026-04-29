# Deploy Patterns

Common deployment patterns using sandboxes.

## Web App
Upload code, start a server, expose a port.
- Script: [deploy-web-app.py](../scripts/deploy-web-app.py)
- Lab: [02-deploy-web-app.ipynb](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/01-sandbox-getting-started/02-deploy-web-app.ipynb)

## MCP Server
Clone an MCP server repo, build, expose port, connect from VS Code.
- Script: coming soon
- Lab: coming soon

## Agent IN Sandbox
Agent loop runs inside the sandbox — Copilot CLI, Claude Code, OpenClaw working autonomously.
- Script: coming soon
- Lab: coming soon

## Sandbox AS Tool (Code Interpreter)
Agent runs outside, calls sandbox remotely for code execution.
Fan out across N sandboxes, secrets stay outside, scale to zero.
- Script: coming soon
- Lab: coming soon

## Copilot CLI
Create sandbox, install Copilot CLI, configure Azure OpenAI BYOK.
Supports zero-trust mode where API key is injected via egress rules.
- Script: [copilot-cli-byok.py](../scripts/copilot-cli-byok.py)
- Lab: [03-copilot-cli.ipynb](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/01-sandbox-getting-started/03-copilot-cli.ipynb)

