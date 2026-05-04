# OpenAI Agents SDK + Azure Sandboxes

Give an OpenAI Agent its own isolated Linux computer in Azure. The agent uses sandbox `exec`, `write_file`, and `read_file` as `@function_tool`s — so it can read files, run commands, fix bugs, and serve a live web page, all from inside a hardware-isolated Azure sandbox.

## What You'll Do

- `01-agents-getting-started.ipynb` — Wrap the sandbox SDK as agent tools and have the agent introspect its own machine
- `02-agent-coding-task.ipynb` — Drop a failing Python project into the sandbox and let the agent read, edit, and re-test until it passes
- `03-agent-live-preview.ipynb` — Agent serves a public web page from inside the sandbox via ACA's per-port URL

## How to Run

| Notebook | What You Learn |
|----------|---------------|
| [`01-agents-getting-started.ipynb`](./01-agents-getting-started.ipynb) | Wire `azure-containerapps-sandbox` calls as `@function_tool`s and run an `Agent` against them |
| [`02-agent-coding-task.ipynb`](./02-agent-coding-task.ipynb) | The classic AI SWE loop — read `task.md`, edit `src/hello.py`, re-run `pytest`, summarize |
| [`03-agent-live-preview.ipynb`](./03-agent-live-preview.ipynb) | Expose a port, run `python3 -m http.server`, agent announces a public live URL |

## Prerequisites

- Azure CLI: `az login`
- Python SDKs:
  - **`azure-containerapps-sandbox`** — ships via GitHub Releases on this repo, **not** PyPI. The `--output` flag gives the wheel a fixed filename so the next line works in any shell:
    ```bash
    gh release download v0.1.0b1 \
        --repo Azure-Samples/azure-container-apps-sandboxes \
        --pattern "azure_containerapps_sandbox-*.whl" \
        --output azure_containerapps_sandbox.whl
    pip install azure_containerapps_sandbox.whl
    ```
  - **`openai-agents`** — published on PyPI:
    ```bash
    pip install openai-agents
    ```
- An OpenAI provider — configured **once** for all three notebooks. Copy
  [`provider_config.py.example`](./provider_config.py.example) to `provider_config.py`
  in this folder and fill in either the Azure OpenAI or OpenAI section. The
  notebooks import a shared helper ([`provider.py`](./provider.py)) that picks
  it up automatically. `provider_config.py` is gitignored.

  - **Azure OpenAI** (preferred, default): set `AZURE_OPENAI_ENDPOINT` and
    `AZURE_OPENAI_DEPLOYMENT`. Auth is via Entra ID (`DefaultAzureCredential`)
    by default — assign your user the **Cognitive Services OpenAI User** role
    on the AOAI resource. Set `AZURE_OPENAI_API_KEY` only if you'd rather use
    key auth.
  - **OpenAI**: set `OPENAI_API_KEY`; optional `OPENAI_MODEL` (default `gpt-4o`).

  If you'd rather not use a config file, leave it as the template and set the
  matching environment variables (`AZURE_OPENAI_*` or `OPENAI_API_KEY`) in
  your shell before launching VS Code. Azure OpenAI wins if both are set.
