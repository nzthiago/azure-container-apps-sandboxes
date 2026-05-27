# langchain — coming soon

A future variant of [`08-sandbox-agents`](../README.md) that wires
[LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)
to an Azure Container Apps sandbox via a
[`BaseSandbox`](https://docs.langchain.com/oss/python/contributing/implement-langchain#sandboxes)
subclass.

Deep Agents' `BaseSandbox` provides the filesystem tools (`ls` /
`read` / `write` / `edit` / `glob` / `grep`) on top of `execute()`
+ `python3`, so the ACA backend only needs to implement `execute`,
`upload_files`, `download_files`, and `id`.

Will follow the same shape as [`openai/`](../openai):

- `aca_sandbox_adapter.py` — `ACASandbox(BaseSandbox)` against the
  Deep Agents sandbox-backend protocol.
- `agent.py` — boot sandbox → wire backend → run one turn → cleanup.

Track progress in [`samples/sandboxes/README.md`](../../../README.md).
