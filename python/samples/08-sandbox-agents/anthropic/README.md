# anthropic — coming soon

A future variant of [`08-sandbox-agents`](../README.md) that wires
[Claude Managed Agents — self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
to an Azure Container Apps sandbox.

The agent loop runs on Anthropic's infrastructure; tool execution
moves into a fresh ACA sandbox in your subscription via a custom
sandbox client — the same pattern as other managed sandbox providers.

Will follow the same shape as [`openai/`](../openai):

- `aca_sandbox_adapter.py` — `ACASandboxClient` against the Claude
  Managed Agents sandbox-client protocol.
- `agent.py` — boot sandbox → wire adapter → run one turn → cleanup.

Track progress in [`samples/sandboxes/README.md`](../../../README.md).
