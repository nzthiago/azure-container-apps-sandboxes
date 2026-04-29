# Labs

Hands-on Jupyter notebooks for Azure Container Apps Sandboxes. Each lab is independent —
pick any one and run it.

Sandboxes are secure, isolated, ephemeral compute environments with sub-second startup,
snapshot/resume, and scale-to-zero. Learn more in the [README](../README.md).

## How to Run

```bash
# Install SDKs
pip install azure-sandbox azure-mgmt-sandbox

# Open any notebook in VS Code and run step by step
```

## Sandbox Labs

| Lab | Notebook | What You Learn |
|-----|----------|---------------|
| Getting Started | [01-getting-started.ipynb](01-sandbox-getting-started/01-getting-started.ipynb) | Full lifecycle: group + sandbox + exec + port + snapshot + stop + resume |
| Deploy Web App | [02-deploy-web-app.ipynb](01-sandbox-getting-started/02-deploy-web-app.ipynb) | Upload code, start server, expose port, test public URL |
| Copilot CLI (BYOK) | [03-copilot-cli.ipynb](01-sandbox-getting-started/03-copilot-cli.ipynb) | BYOK Azure OpenAI, zero-trust egress, offline mode, SSH |
