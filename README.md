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

## Catalog

### Get Started with API essentials

| # | Guide | What it shows | CLI | Python | Status |
|---|---|---|---|---|---|
| 00 | sandbox-groups | Create group, assign role, run sandbox, delete group | [CLI](cli/samples/00-get-started/00-sandbox-groups) | [Python](python/samples/00-get-started/00-sandbox-groups) | ✅ ready |
| 01 | sandboxes | Basic + advanced + parallel (asyncio) + YAML apply, all in one script | [CLI](cli/samples/00-get-started/01-sandboxes) | [Python](python/samples/00-get-started/01-sandboxes) | ✅ ready |
| 02 | snapshots | `create_snapshot`, restore into new sandbox | [CLI](cli/samples/00-get-started/02-snapshots) | [Python](python/samples/00-get-started/02-snapshots) | ✅ ready |
| 03 | disks | Build from container image **and** commit running sandbox to a disk (combined) | [CLI](cli/samples/00-get-started/03-disks) | [Python](python/samples/00-get-started/03-disks) | ✅ ready |
| 04 | volumes | AzureBlob shared mounts across sandboxes | [CLI](cli/samples/00-get-started/04-volumes) | [Python](python/samples/00-get-started/04-volumes) | ✅ ready |
| 05 | lifecycle | stop / resume + AutoSuspendPolicy + AutoDeletePolicy | [CLI](cli/samples/00-get-started/05-lifecycle) | [Python](python/samples/00-get-started/05-lifecycle) | ✅ ready |
| 06 | ports | `add_port(anonymous=True)`, hit public URL | [CLI](cli/samples/00-get-started/06-ports) | [Python](python/samples/00-get-started/06-ports) | ✅ ready |
| 07 | files | write / read / stat / list / mkdir / delete | [CLI](cli/samples/00-get-started/07-files) | [Python](python/samples/00-get-started/07-files) | ✅ ready |
| 08 | egress | `set_egress_default("Deny")` + host allow rules | [CLI](cli/samples/00-get-started/08-egress) | [Python](python/samples/00-get-started/08-egress) | ✅ ready |
| 09 | secrets | upsert / peek / list / delete (group-scoped) | [CLI](cli/samples/00-get-started/09-secrets) | [Python](python/samples/00-get-started/09-secrets) | ✅ ready |
| 10 | identity | Group identity (SystemAssigned / UserAssigned managed identity today; extensible) | [CLI](cli/samples/00-get-started/10-identity) | [Python](python/samples/00-get-started/10-identity) | ✅ ready |
| 11 | labels | `labels=` on create + `list_sandboxes(labels=…)` | [CLI](cli/samples/00-get-started/11-labels) | [Python](python/samples/00-get-started/11-labels) | ✅ ready |
| 12 | interactive-shell | `aca sandbox shell` — interactive PTY session (CLI only) | [CLI](cli/samples/00-get-started/12-interactive-shell) | — | ✅ ready |


### Scenarios — composed use cases (with production tips)

| # | Scenario | What it will show | CLI | Python | Status |
|---|---|---|---|---|---|
| 01 | webapps | Run a web app in a sandbox; patterns include `simple-anonymous` (open to the internet) and (planned) `authenticated` (Entra-gated) | [CLI](cli/samples/01-webapps) | [Python](python/samples/01-webapps) | ✅ ready |
| 02 | coding-agents | Run **Copilot CLI** in a sandbox with deny-default egress + portal-paste PAT injection (Python + CLI). Claude Code / Codex stubs included. | [CLI](cli/samples/02-coding-agents) | [Python](python/samples/02-coding-agents) | ✅ Copilot CLI ready |
| 03 | code-interpreter | LLM-driven code execution — generate, run, observe, iterate | [CLI](cli/samples/03-code-interpreter) | [Python](python/samples/03-code-interpreter) | 📝 planned |
| 04 | swarms | Orchestrator coordinating many sandbox workers — variants 01 (sandbox inception: orchestrator sandbox spawns workers in another group via its group's MI) and 02 (same plus an AzureBlob volume as durable shared scratchpad) ship now | [CLI](cli/samples/04-swarms) | [Python](python/samples/04-swarms) | ✅ ready |
| 05 | data-processing | Producer/consumer pipelines on shared AzureBlob volumes | [CLI](cli/samples/05-data-processing) | [Python](python/samples/05-data-processing) | 📝 planned |
| 06 | developer-workflows | PR builds, ephemeral CI, on-demand dev environments | [CLI](cli/samples/06-developer-workflows) | [Python](python/samples/06-developer-workflows) | 📝 planned |
| 07 | computer-use | LLM computer-use agent (Azure OpenAI `computer-use-preview` / gpt-5.4) driving Chrome inside a sandbox to fill out a form or any web task; watch live via noVNC. Built on the OpenAI Agents SDK (`AsyncComputer` + `ComputerTool`). | [CLI](cli/samples/07-computer-use) | [Python](python/samples/07-computer-use) | ✅ OpenAI ready |
| 08 | sandbox-agents | Agent frameworks (OpenAI Agents SDK, Claude Managed Agents, LangChain Deep Agents) using ACA sandboxes as their tool-execution backend. OpenAI ships a **first-class provider package** (`agents_aca_sandboxes`) plus a live Deep Research demo and a platform-architecture brief. | [CLI](cli/samples/08-sandbox-agents) | [Python](python/samples/08-sandbox-agents) | ✅ OpenAI provider + demo |
| 09 | mcp-hosting | Host MCP servers in a sandbox — `excalidraw-anonymous` (public via `add_port`) and `dab-sql-devtunnel` (DAB + Postgres + Chinook, exposed via Dev Tunnels with **no inbound port** on the sandbox) | [CLI](cli/samples/09-mcp-hosting) | [Python](python/samples/09-mcp-hosting) | ✅ Python ready · 📝 CLI planned |

## Reference

- [Python SDK README](https://github.com/microsoft/azure-container-apps/blob/main/docs/early/python-sdk/README.md)
- [ACA CLI README](https://github.com/microsoft/azure-container-apps/blob/main/docs/early/aca-cli/README.md)
