# Sandboxes

Isolated, on-demand VMs for AI agents and code execution.

## Prerequisites

Make sure you have all of the following before running any lab:

| | Required for | Install / docs |
|---|---|---|
| **Azure subscription** | everything | one with permission to create resource groups and assign roles |
| **Azure CLI** (`az`) | everything - used to authenticate | <https://learn.microsoft.com/cli/azure/install-azure-cli> |
| **`az login` completed** | everything | run `az login` once after installing the CLI |
| **Python 3.10+** + `pip` | Python guides + `setup/python/setup.py` | <https://www.python.org/downloads/> |
| **Bash** | CLI guides + `setup/cli/setup.sh` | built-in on Linux/macOS; on Windows use Git Bash, WSL, or MSYS2 |
| **`aca` CLI** | CLI guides | installed automatically by `setup/cli/setup.sh`, or follow <https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md> |
| **`curl`** | the install script that pulls down `aca` | usually already present; on Windows it ships with Git for Windows / WSL |

You only need **one** of Python or Bash - pick the flow that matches the
guides you'll run. Both flows produce the same `samples/.env`, so you
can mix freely later.

## Quickstart

One-time baseline (resource group + sandbox group + RBAC). Pick the
flow that matches what you'll use the most - both write the same
`samples/.env` so you can switch freely later.

```bash
# Python SDK flow (needs Python 3.10+)
cd setup/python
pip install -r requirements.txt
python setup.py

# OR: aca CLI flow (no Python required)
cd setup/cli
./setup.sh
```

> On Windows, run from Git Bash, WSL, MSYS2 - any shell with `bash`.

Then run a sample - cd into any folder under `guides/` or `scenarios/`:

```bash
cd guides/01-sandboxes/python
pip install -r requirements.txt
python sandboxes.py
```

See [`setup/README.md`](setup/README.md) for the full setup
documentation and how to override defaults.

## Samples

### Labs - guided Jupyter notebooks (Python SDK)

Step-through notebooks that run the Python SDK end-to-end. Open in VS Code and **Run All**, or step cell by cell to inspect each output.

| # | Lab | What it shows |
|---|---|---|
| 01 | [getting-started](python/labs/01-getting-started.ipynb) | Full surface end-to-end: create group → sandbox from disk → exec → files → ports → egress → lifecycle → cleanup |
| 02 | [bring-your-own-container](python/labs/02-bring-your-own-container.ipynb) | Build a sandbox from your own container image and open a port to access its web content |
| 03 | [sandbox-inception](python/labs/03-sandbox-inception.ipynb) | Run the SDK **inside** a sandbox to spawn and manage child sandboxes using the group's managed identity - no secrets |

### Scenarios - composed use cases (with production tips)

| # | Scenario | What it will show | Python | CLI |
|---|---|---|---|---|
| 01 | webapps | Run a web app in a sandbox; patterns include `simple-anonymous` (open to the internet) and (planned) `authenticated` (Entra-gated) | [Python](python/samples/01-webapps) | [CLI](cli/samples/01-webapps) |
| 02 | coding-agents | Run **Copilot CLI** in a sandbox with deny-default egress + portal-paste PAT injection (Python + CLI). Claude Code / Codex stubs included. | [Python](python/samples/02-coding-agents) | [CLI](cli/samples/02-coding-agents) |
| 03 | code-interpreter | LLM-driven code execution - generate, run, observe, iterate | [Python](python/samples/03-code-interpreter) | - |
| 04 | swarms | Orchestrator coordinating many sandbox workers - variants 01 (sandbox inception: orchestrator sandbox spawns workers in another group via its group's MI) and 02 (same plus an AzureBlob volume as durable shared scratchpad) ship now | [Python](python/samples/04-swarms) | - |
| 05 | data-processing | Producer/consumer pipelines on shared AzureBlob volumes | [Python](python/samples/05-data-processing) | - |
| 06 | developer-workflows | PR builds, ephemeral CI, on-demand dev environments | [Python](python/samples/06-developer-workflows) | - |
| 07 | computer-use | LLM computer-use agent (Azure OpenAI `computer-use-preview` / gpt-5.4) driving Chrome inside a sandbox to fill out a form or any web task; watch live via noVNC. Built on the OpenAI Agents SDK (`AsyncComputer` + `ComputerTool`). | - | - |
| 08 | sandbox-agents | Agent frameworks (OpenAI Agents SDK, Claude Managed Agents, LangChain Deep Agents) using ACA sandboxes as their tool-execution backend. OpenAI ships a **first-class provider package** (`agents_aca_sandboxes`) plus a live Deep Research demo and a platform-architecture brief. | [Python](python/samples/08-sandbox-agents) | - |
| 09 | mcp-hosting | Host MCP servers in a sandbox - `excalidraw-anonymous` (public via `add_port`) and `dab-sql-devtunnel` (DAB + Postgres + Chinook, exposed via Dev Tunnels with **no inbound port** on the sandbox) | [Python](python/samples/09-mcp-hosting) | - |
| 10 | connectors-email-triage | **Connector Namespaces + ACA Sandbox**: Outlook `When a new email arrives (V3)` trigger → ACA receiver → per-email sandbox → GitHub Copilot CLI → Teams MCP (Work IQ) posts a triage card. End-to-end `azd up`, deny-default egress + Transform-rule API-key stamping. | [Python](python/samples/10-connectors-email-triage) | - |
| 11 | connectors-document-automation | **ACA Sandbox as direct webhook target** for a Connector Namespaces SharePoint trigger. No receiver, no Function host. Sandbox runs FastAPI on :8080, Copilot CLI uses Work IQ SharePoint MCP + `pdftotext`/`tesseract` to extract invoice data and upload results back to SharePoint. End-to-end `azd up`. | [Python](python/samples/11-connectors-document-automation) | - |

## Reference

- [Python SDK README](https://github.com/microsoft/azure-container-apps/blob/main/docs/early/python-sdk/README.md)
- [ACA CLI README](https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md)
