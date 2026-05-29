# Sandboxes

Isolated, on-demand VMs for AI agents and code execution.

## Prerequisites

Make sure you have all of the following before running any lab:

| | Required for | Install / docs |
|---|---|---|
| **Azure subscription** | everything | one with permission to create resource groups and assign roles |
| **Azure CLI** (`az`) | everything — used to authenticate | <https://learn.microsoft.com/cli/azure/install-azure-cli> |
| **`az login` completed** | everything | run `az login` once after installing the CLI |
| **Python 3.10+** + `pip` | Python guides + `setup/python/setup.py` | <https://www.python.org/downloads/> |
| **Bash** | CLI guides + `setup/cli/setup.sh` | built-in on Linux/macOS; on Windows use Git Bash, WSL, or MSYS2 |
| **`aca` CLI** | CLI guides | installed automatically by `setup/cli/setup.sh`, or follow <https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md> |
| **`curl`** | the install script that pulls down `aca` | usually already present; on Windows it ships with Git for Windows / WSL |

You only need **one** of Python or Bash — pick the flow that matches the
guides you'll run. Both flows produce the same `samples/.env`, so you
can mix freely later.

## Quickstart

One-time baseline (resource group + sandbox group + RBAC). Pick the
flow that matches what you'll use the most — both write the same
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

> On Windows, run from Git Bash, WSL, MSYS2 — any shell with `bash`.

Then run a sample — cd into any folder under `guides/` or `scenarios/`:

```bash
cd guides/01-sandboxes/python
pip install -r requirements.txt
python sandboxes.py
```

See [`setup/README.md`](setup/README.md) for the full setup
documentation and how to override defaults.

## Samples

### Labs — guided Jupyter notebooks (Python SDK)

Interactive, run-all notebooks that walk the full sandbox surface step-by-step:

| # | Lab | What it covers |
|---|---|---|
| 01 | [Getting Started](python/labs/01-getting-started.ipynb) | End-to-end lifecycle: create a sandbox group → sandbox → exec → files → ports → snapshots → disks → volumes → lifecycle policies → egress rules → secrets → cleanup |
| 02 | [Bring Your Own Container](python/labs/02-bring-your-own-container.ipynb) | Build and run custom container images as sandbox disk images |

### Scenarios — composed use cases (with production tips)

| # | Scenario | What it will show | CLI | Python | Status |
|---|---|---|---|---|---|
| 01 | webapps | Run a web app in a sandbox; patterns include `simple-anonymous` (open to the internet) and (planned) `authenticated` (Entra-gated) | [CLI](cli/samples/01-webapps) | [Python](python/samples/01-webapps) | ✅ ready |
| 02 | coding-agents | Run **Copilot CLI** in a sandbox with deny-default egress + portal-paste PAT injection (Python + CLI). Claude Code / Codex stubs included. | — | — | 🔜 coming soon |
| 03 | code-interpreter | LLM-driven code execution — generate, run, observe, iterate | — | — | 🔜 coming soon |
| 04 | swarms | Orchestrator coordinating many sandbox workers — variants 01 (sandbox inception: orchestrator sandbox spawns workers in another group via its group's MI) and 02 (same plus an AzureBlob volume as durable shared scratchpad) ship now | — | — | 🔜 coming soon |
| 05 | data-processing | Producer/consumer pipelines on shared AzureBlob volumes | — | — | 🔜 coming soon |
| 06 | developer-workflows | PR builds, ephemeral CI, on-demand dev environments | — | — | 🔜 coming soon |
| 07 | computer-use | LLM computer-use agent (Azure OpenAI `computer-use-preview` / gpt-5.4) driving Chrome inside a sandbox to fill out a form or any web task; watch live via noVNC. Built on the OpenAI Agents SDK (`AsyncComputer` + `ComputerTool`). | — | — | 🔜 coming soon |
| 08 | sandbox-agents | Agent frameworks (OpenAI Agents SDK, Claude Managed Agents, LangChain Deep Agents) using ACA sandboxes as their tool-execution backend. OpenAI ships a **first-class provider package** (`agents_aca_sandboxes`) plus a live Deep Research demo and a platform-architecture brief. | — | — | 🔜 coming soon |
| 09 | mcp-hosting | Host MCP servers in a sandbox — `excalidraw-anonymous` (public via `add_port`) and `dab-sql-devtunnel` (DAB + Postgres + Chinook, exposed via Dev Tunnels with **no inbound port** on the sandbox) | — | — | 🔜 coming soon |

## Reference

- [Python SDK README](https://github.com/microsoft/azure-container-apps/blob/main/docs/early/python-sdk/README.md)
- [ACA CLI README](https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md)
