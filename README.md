# Azure Sandboxes

Azure Container Apps Sandboxes is a first-class resource type in Azure Container Apps that provides fast, secure, ephemeral compute environments with built-in suspend and resume capabilities. Sandboxes join the Container Apps family alongside Apps, Jobs, and Dynamic Sessions as a foundational building block for the next generation of cloud workloads.

## What Are ACA Sandboxes?

A Sandbox is a secure, isolated compute environment that can be created, used, suspended, and resumed on demand. Sandboxes are organized under **Sandbox Groups** (`Microsoft.App/SandboxGroups`), which let you manage collections of sandboxes with shared configuration.

**Key capabilities:**

- **Sub-second startup** — provisioned from prewarmed pools for near-instant availability
- **Strong isolation** — each sandbox runs in its own secure boundary, enterprise-grade security for untrusted code
- **Scale to zero** — pay nothing when idle, resources consumed only while actively running
- **Massive scale-out** — burst to thousands of concurrent sandboxes without manual intervention
- **OCI container images** — bring your own container image with your preferred runtime and tools
- **Snapshots** — suspend a sandbox capturing full memory and disk state, resume later in sub-second time

## Use Cases

| Scenario | How Sandboxes Help |
|----------|-------------------|
| AI code execution | Safely run LLM-generated code in isolated environments with instant startup |
| Development environments | On-demand, suspendable dev environments that preserve state across sessions |
| SaaS platforms | Isolated, per-tenant environments that start instantly and suspend when idle |
| Agent workflows | Persistent, isolated workspaces for AI agents that survive across task boundaries |
| CI/CD pipelines | Ephemeral build and test environments that scale to zero when idle |
| Burst workloads | Scale from zero to thousands of sandboxes in response to demand |

## This Repository

This repo contains developer tools, plugins, and tutorials for ACA Sandboxes:

- **Plugin Store** — Copilot CLI and Claude Code plugins with the `azure-sandbox` skill
- **Labs** — Hands-on Jupyter notebook tutorials
- **Release artifacts** — Python SDK wheels and ACA CLI npm package published through GitHub Releases

## Install

### Plugin (Copilot CLI / Claude Code)

```bash
# Copilot CLI — add the marketplace, then install the sandbox skill
/plugin marketplace add Azure-Samples/azure-container-apps-sandboxes
/plugin install azure-sandbox@azure-container-apps

# Claude Code
claude plugin add Azure-Samples/azure-container-apps-sandboxes
```

### Python SDK

```bash
# From GitHub Release
gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_containerapps_sandbox-*.whl" --dir /tmp
pip install /tmp/azure_containerapps_sandbox-*.whl
```

### ACA CLI

```bash
npm install -g https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases/download/v0.1.0b1/azure-containerapps-cli-1.0.0-beta.1.tgz
```

### Uninstall

```bash
npm uninstall -g @azure/containerapps-cli
pip uninstall azure-containerapps-sandbox
```

## SDK Usage

```python
from azure.containerapps.sandbox import SandboxClient, SandboxGroupClient

client = SandboxClient(resource_group="my-rg")
mgmt = SandboxGroupClient(resource_group="my-rg")
```

Use `mgmt` (`SandboxGroupClient`) for sandbox group operations (create/delete groups) and `client` for sandbox operations (create, exec, files, ports, snapshots, etc.). For end-to-end examples, see the notebooks in [`labs/`](labs/).

## Skills

| Skill | Description |
|-------|-------------|
| [azure-sandbox](plugin/skills/azure-sandbox/SKILL.md) | Manage sandbox groups and sandboxes — create, exec, shell, files, ports, egress, images, snapshots, stop/resume |

## Labs

| Lab | Notebook | What You Learn |
|-----|----------|----------------|
| Getting Started | [01-getting-started.ipynb](labs/01-sandbox-getting-started/01-getting-started.ipynb) | Full lifecycle: group → sandbox → exec → files → port → snapshot → stop → resume → cleanup |
| Deploy Web App | [02-deploy-web-app.ipynb](labs/01-sandbox-getting-started/02-deploy-web-app.ipynb) | Upload code, start server, expose port, test public URL |
| Copilot CLI (BYOK) | [03-copilot-cli.ipynb](labs/01-sandbox-getting-started/03-copilot-cli.ipynb) | BYOK Azure OpenAI, zero-trust egress, offline mode |

## Portal

Manage sandboxes visually at [containerapps.azure.com](https://containerapps.azure.com/sandbox-groups):

- [Browse Sandbox Groups](https://containerapps.azure.com/sandbox-groups)
- [Create Sandbox Group](https://containerapps.azure.com/sandbox-groups/create)

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for general guidance.

### Adding a Skill

Skills live in `plugin/skills/<skill-name>/` and include:

- `SKILL.md` — Skill description, install instructions, usage examples, and references
- `references/` — Supplementary docs, runbooks, and setup guides

To add a new skill, create a directory under `plugin/skills/`, add a `SKILL.md` with install/usage instructions, and register it in [`marketplace.json`](marketplace.json).

### Adding a Lab

Labs live in `labs/<topic>/` as Jupyter notebooks (`.ipynb`). Each lab should:

- Be self-contained — runnable with just `az login` and the SDK installed
- Include setup, step-by-step walkthrough, and cleanup cells
- Use `SandboxClient` and `SandboxGroupClient` from the current SDK

To add a new lab, create a directory under `labs/`, add your notebooks, and update [`labs/README.md`](labs/README.md).

## Release

Release upload workflow: [`scripts/release.sh`](scripts/release.sh)

```bash
./scripts/release.sh v0.1.0b1 /path/to/dist
```

## Links

- [Portal](https://containerapps.azure.com/sandbox-groups)
- [Pricing](https://azure.microsoft.com/pricing/details/container-apps/)

## License

[MIT](LICENSE.md)
