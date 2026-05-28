# 10 - Identity

How a sandbox group authenticates to the rest of Azure. Today that
means a System-assigned or User-assigned **managed identity** on the
group; new identity options will land in this guide as they ship. The
identity's `principalId` can be granted any Azure RBAC role — most
commonly **Container Apps SandboxGroup Data Owner** on a *different*
sandbox group, enabling cross-group orchestration without shipping
client credentials into the sandbox.

- [`python/`](python/) — `mgmt.create_group(name, location, identity={"type": "SystemAssigned"})` + `patch_group_identity(...)`
- [`cli/`](cli/) — `aca sandboxgroup identity assign --name X --system-assigned`

## What's covered

| API | Python | CLI |
|---|---|---|
| Create group with SystemAssigned MI | `create_group(name, location, identity={"type": "SystemAssigned"})` | `sandboxgroup create --name X ... --identity-type SystemAssigned` |
| Add MI to existing group | `patch_group_identity(name, {"type": "SystemAssigned"})` | `sandboxgroup identity assign --name X --system-assigned` |
| Read MI principalId | `get_group(name).identity` | `sandboxgroup identity show --name X` |
| Remove MI | `patch_group_identity(name, {"type": "None"})` | `sandboxgroup identity remove --name X` |
| User-assigned MI | `identity={"type": "UserAssigned", "userAssignedIdentities": {"<resource-id>": {}}}` | `--user-assigned <resource-id>` |

## Demo flow

This guide creates a **temporary sandbox group** with a SystemAssigned
identity (so it doesn't touch the shared `samples` group), prints the
identity, and tears it down. No role assignment in this guide — see
the **swarms** scenario for the full
"orchestrator-in-group-A creates workers-in-group-B" pattern that
requires granting the orchestrator's MI a Data Owner role on group B.

## Why this matters

- **Per-sandbox secrets free**: instead of mounting credentials into a
  sandbox, the sandbox uses its group's MI (via
  `azure.identity.ManagedIdentityCredential` from inside) to obtain
  tokens for Azure services.
- **Agent inception**: one sandbox group's MI is the trust anchor for
  another sandbox group — agents can spawn sub-agents in isolated
  environments without secrets.
