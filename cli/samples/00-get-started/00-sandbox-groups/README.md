# 00 - Sandbox groups

Walk through the full provisioning lifecycle of a sandbox group:

1. **Create the group** - ARM control plane.
2. **List + get** the group - the standard `create / list / get` triad
   you'll see at the top of every guide in this catalog.
3. **Assign the role** - `Container Apps SandboxGroup Data Owner` to
   the current principal at the group scope.
4. **Use the data plane** - create a sandbox, exec, delete.
5. **Tear it down** - delete the group.

This is the same flow `samples/sandboxes/setup/python/setup.py` (and its
CLI counterpart) runs once for the shared group. Working through it
yourself makes it obvious what RBAC and management calls are needed
before any data-plane SDK or `aca sandbox` call will work.

The guide uses a unique throwaway group name (`guide-00-<short-id>`)
so it never collides with the shared `ai-apps-samples-group`. Only the
resource group and region are reused from `samples/.env`.

Choose your style:

- [`python/`](python/) - Python SDK (`SandboxGroupManagementClient` +
  `azure-mgmt-authorization` + `SandboxGroupClient`).
- [`cli/`](cli/) - `aca` CLI (`aca sandboxgroup create`, `aca
  sandboxgroup role create`, `aca sandbox ...`, `aca sandboxgroup delete`).

> The SDK and CLI both transparently retry 403s for ~100s during role
> propagation, so the first data-plane call succeeds without a manual
> sleep after the role assignment.

## What you'll see

```
==> Subscription:   ...
    Resource group: ai-apps-samples-rg
    Region:         westus2
    Sandbox group:  guide-00-a3f29b1c  (will be deleted at end)
==> Creating sandbox group 'guide-00-a3f29b1c' in westus2...
==> Assigning 'Container Apps SandboxGroup Data Owner'...
==> Creating sandbox in the new group...
    sandbox: 0139...
==> Running command in sandbox...
hello from adc-sandbox
==> Deleting sandbox 0139...
==> Deleting sandbox group 'guide-00-a3f29b1c'...
==> Done.
```
