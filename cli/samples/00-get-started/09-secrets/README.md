# 09 - Secrets

Group-scoped secrets: upsert, list, list keys, peek, delete.

Secrets in this product live on the **sandbox group**, not the sandbox.
The same secret is available to all sandboxes in the group. Your app
code (running inside a sandbox) fetches the secret value via the SDK
at runtime — secrets are *not* auto-injected as env vars.

- [`python/`](python/) — `group_client.upsert_secret(...)` / `.peek_secret(...)`
- [`cli/`](cli/) — `aca sandboxgroup secret upsert --name K --values "K1=V1,K2=V2"`

## What's covered

| API | Python | CLI |
|---|---|---|
| Upsert | `upsert_secret(id, {"KEY": "VAL"})` | `secret upsert --name X --values "K=V"` |
| List | `list_secrets()` | `secret list` |
| List keys | `list_secret_keys(id)` | (use `peek` JSON output) |
| Peek values | `peek_secret(id)` | (Python only) |
| Delete | `delete_secret(id)` | `secret delete --name X` |

## Why this matters

API keys, model credentials, signed tokens — anything an LLM agent or
generated code needs to call external services. Group-scoped keeps you
from re-uploading per sandbox. Pair with **egress allowlists**
([guide 08](../08-egress)) so a compromised secret can only reach
hosts you've blessed.
