# Labs

Hands-on Jupyter notebooks for Azure Container Apps Sandboxes. Each lab is independent —
pick any one and run it.

Sandboxes are secure, isolated, ephemeral compute environments with sub-second startup,
snapshot/resume, and scale-to-zero. Learn more in the [README](../README.md).

## How to Run

```bash
# Install SDK from GitHub Release
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_containerapps_sandbox-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_containerapps_sandbox-*-py3-none-any.whl
# Install `openai-agents` SDK for the OpenAI Agents lab)
pip install azure-containerapps-sandbox

# Open any notebook in VS Code and run step by step
```

## Sandbox Labs

| Lab | Notebook | What You Learn |
|-----|----------|---------------|
| Getting Started | [01-getting-started.ipynb](01-sandbox-getting-started/01-getting-started.ipynb) | Full lifecycle: group + sandbox + exec + port + snapshot + stop + resume |
| Deploy Web App | [02-deploy-web-app.ipynb](01-sandbox-getting-started/02-deploy-web-app.ipynb) | Upload code, start server, expose port, test public URL |
| Copilot CLI (BYOK) | [03-copilot-cli.ipynb](01-sandbox-getting-started/03-copilot-cli.ipynb) | BYOK Azure OpenAI, zero-trust egress, offline mode |
| OpenAI Agents — Getting Started | [01-agents-getting-started.ipynb](02-openai-agents-sandbox/01-agents-getting-started.ipynb) | Wrap the sandbox SDK as `@function_tool`s and run an OpenAI Agent against them |
| OpenAI Agents — Coding Task | [02-agent-coding-task.ipynb](02-openai-agents-sandbox/02-agent-coding-task.ipynb) | Agent reads `task.md`, edits `src/hello.py`, re-runs `pytest` until it passes |
| OpenAI Agents — Live Preview | [03-agent-live-preview.ipynb](02-openai-agents-sandbox/03-agent-live-preview.ipynb) | Agent serves a public web page from inside the sandbox via a per-port URL |
