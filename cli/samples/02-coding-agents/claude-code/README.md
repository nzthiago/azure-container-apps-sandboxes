# claude-code — coming soon

A future variant of [`02-coding-agents`](../README.md) that runs
Anthropic's Claude Code CLI inside a sandbox using the same pattern:
deny-default egress + host allows + Transform rules that inject the
Anthropic API key into the `Authorization` header so the key never
enters the sandbox.

Track progress in
[`samples/sandboxes/README.md`](../../../README.md).
