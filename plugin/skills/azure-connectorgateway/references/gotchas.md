# Gotchas & Troubleshooting

Common issues and their solutions.

| Issue | Solution |
|-------|----------|
| Trigger not firing | Ensure access policy exists granting gateway MI access to the connection |
| Gateway can't subscribe | Create an access policy granting the gateway MI access to the connection |
| Sandbox must be Running | For InvokePort targets, sandbox must be running; for ShellCommand, sandbox activates on demand |
| Port auth for InvokePort | Add gateway's principalId to the port's entraId objectIds on the sandbox |
| Cleanup order | Delete trigger config → connection → sandbox → gateway |
| SandboxGroupNotFound 404 | Data plane propagation after ARM group creation can take **5–20+ minutes**. Use retry with 30-140s waits, up to 12 attempts. **Better: reuse existing sandbox groups** |
| Sandbox state field wrong path | State is at `sbx['state']` (top level), NOT `sbx['properties']['state']` — data plane returns flat JSON |
| Sandbox identity not found | Identity (principalId/tenantId) is on the **sandbox group**, not individual sandboxes. Use `group['identity']['principalId']` |
| `dynamicInvoke` 400: `parameters` not valid | Use `{"request": {"method": ..., "path": ...}}` format, NOT `{"parameters": {"operationId": ...}}` |
| `dynamicInvoke` 400: `Content-*` headers | Do NOT include `Content-Type` or other `Content-*` headers in the request object |
| `dynamicInvoke` returns `NotFound` for POST | Ensure you pass `queries` and `body` in the request object |
| `list_operations` AttributeError | Use `az rest --method POST .../{gw}/listOperations --body '{"connectorName": "..."}'` or `az connectorgateway trigger operations list` |
| Runtime URL 403: missing ACL | Create access policy granting caller's principalId access to the connection |
| Consent redirect shows error | Use `--redirect-url "https://microsoft.com"` — default redirect (`global.consent.azure-apim.net/redirect`) is broken. Consent is auto-confirmed during the `/confirm` step; no code pasting needed |
| Connection stuck in "Error" | User may not have completed browser auth. Re-generate consent link |
| `dynamicInvoke` browse fails (mangled JSON) | Use `@file` pattern for `az rest --body` when IDs contain `!`. Always URL-encode IDs |
| Swagger paths include `/{connectionId}/...` | Strip the prefix — connection context is already set by the endpoint |
| ShellCommand trigger 403 on callback | Gateway MI needs "Dev Compute SandboxGroup Data Owner" role (`c24cf47c-5077-412d-a19c-45202126392c`) on sandbox group |
| `trigger create --command` KeyError | CLI bug: `command` is reserved kwarg. Use Python SDK directly as workaround |
