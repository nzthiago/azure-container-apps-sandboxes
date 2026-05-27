# codex — coming soon

A future variant of [`02-coding-agents`](../README.md) that runs
OpenAI's Codex CLI inside a sandbox using the same pattern:
deny-default egress + host allows + Transform rules that inject the
OpenAI API key into the `Authorization` header so the key never enters
the sandbox.

Track progress in
[`samples/sandboxes/README.md`](../../../README.md).
