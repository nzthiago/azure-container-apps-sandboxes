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

# Install SDKs from PyPI
pip install azure-containerapps-sandbox

# Open any notebook in VS Code and run step by step
```

For the Durable Task Workflows lab only, also install the official `az durabletask` extension and the optional `durabletask-azuremanaged` Python package. See [`02-durable-task-workflows/README.md`](02-durable-task-workflows/README.md) for the DTS-specific setup.

## Sandbox Labs

| Lab | Notebook | What You Learn |
|-----|----------|---------------|
| Getting Started | [01-getting-started.ipynb](01-sandbox-getting-started/01-getting-started.ipynb) | Full lifecycle: group + sandbox + exec + port + snapshot + stop + resume |
| Deploy Web App | [02-deploy-web-app.ipynb](01-sandbox-getting-started/02-deploy-web-app.ipynb) | Upload code, start server, expose port, test public URL |
| Copilot CLI (BYOK) | [03-copilot-cli.ipynb](01-sandbox-getting-started/03-copilot-cli.ipynb) | BYOK Azure OpenAI, zero-trust egress, offline mode |
| Durable Task Workflows | [01-orchestrate-sandbox-jobs.ipynb](02-durable-task-workflows/01-orchestrate-sandbox-jobs.ipynb) | Sample DTS orchestration for sandbox jobs; use the official `az durabletask` extension for scheduler and task hub lifecycle |
