# 08 - Egress

Lock down outbound network access from a sandbox. Two-step pattern:

1. `set_egress_default("Deny")` — block everything by default
2. `add_egress_host_rule("*.github.com", action="Allow")` — open only what you need

Verify by `curl`-ing both an allowed and a denied host from inside the
sandbox.

- [`python/`](python/) — Python SDK (`sandbox.set_egress_default`, `sandbox.add_egress_host_rule`)
- [`cli/`](cli/) — `aca sandbox egress set --default Deny --host-allow "*.github.com"`

## What's covered

| API | Python | CLI |
|---|---|---|
| Set default action | `sandbox.set_egress_default("Deny")` | `aca sandbox egress set --default Deny` |
| Add host allow rule | `sandbox.add_egress_host_rule("*.github.com", action="Allow")` | `--host-allow "*.github.com"` |
| Read current policy | `sandbox.get_egress_policy()` | `aca sandbox egress show` |
| Audit decisions | `sandbox.get_egress_decisions()` | `aca sandbox egress decisions` |

## Why this matters

Sandboxes that run untrusted code (LLM-generated scripts, customer
plugins, third-party agents) need egress allowlists so they can hit
*your* APIs but not exfiltrate data to arbitrary hosts. Default-deny
+ explicit allowlist is the standard secure pattern.
