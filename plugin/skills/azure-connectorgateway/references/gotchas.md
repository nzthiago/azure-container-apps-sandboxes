# Gotchas & Troubleshooting

Common issues and their solutions.

| Issue | Solution |
|-------|----------|
| Trigger not firing | Ensure access policy exists granting gateway MI access to the connection |
| Gateway can't subscribe | Create an access policy granting the gateway MI access to the connection |
| Sandbox must be Running | For InvokePort targets, sandbox must be running; for ShellCommand, sandbox activates on demand |
| Port auth for InvokePort | Add gateway's principalId to the port's entraId objectIds on the sandbox |
| Cleanup order | Delete trigger config → access policies → connection → sandbox → gateway. Always delete triggers first (they hold subscriptions). |
| SandboxGroupNotFound 404 | Data plane propagation after ARM group creation can take **5–20+ minutes**. Use retry with 30-140s waits, up to 12 attempts. **Better: reuse existing sandbox groups** |
| Sandbox state field wrong path | State is at `sbx['state']` (top level), NOT `sbx['properties']['state']` — data plane returns flat JSON |
| Sandbox identity not found | Identity (principalId/tenantId) is on the **sandbox group**, not individual sandboxes. Use `group['identity']['principalId']` |
| `dynamicInvoke` 400: `parameters` not valid | Use `{"request": {"method": ..., "path": ...}}` format, NOT `{"parameters": {"operationId": ...}}` |
| `dynamicInvoke` 400: `Content-*` headers | Do NOT include `Content-Type` or other `Content-*` headers in the request object |
| `dynamicInvoke` returns `NotFound` for POST | Ensure you pass `queries` and `body` in the request object |
| Runtime URL 403: missing ACL | Create access policy granting caller's principalId access to the connection |
| Consent redirect shows error | Body MUST use `parameters` array format: `{"parameters":[{"objectId":"...","tenantId":"...","redirectUrl":"https://microsoft.com","parameterName":"token"}]}`. Get objectId/tenantId from connection's `authenticatedUser`. Always use `Start-Process` to open the link |
| Connection stuck in "Error" | User may not have completed browser auth. Re-generate consent link with `Start-Process`. Do NOT retry with different body formats |
| `dynamicInvoke` browse fails (mangled JSON) | Use `@file` pattern for `az rest --body` when IDs contain `!`. Always URL-encode IDs |
| Swagger paths include `/{connectionId}/...` | Strip the prefix — connection context is already set by the endpoint |
| ShellCommand trigger 403 on callback | Gateway MI needs "Dev Compute SandboxGroup Data Owner" role (`c24cf47c-5077-412d-a19c-45202126392c`) on sandbox group |
| SDK import `ModuleNotFoundError` | Try `from sandbox import SandboxClient` first, then `from azure.containerapps.sandbox import SandboxClient`. Package name varies by install method |
| `create_trigger()` SDK broken schema | SDK uses wrong body structure (`callbackTarget` which doesn't exist). Use `az rest` with correct schema: `metadata` + `notificationDetails` (callbackUrl/body/auth) + `operationName` + `parameters` at properties root. |
| `exec_command` "no such file" | `exec_command` treats whole string as binary path. Use `aca sandbox exec -c "python /app/handler.py"` (shell-interpreted) instead |
| `az rest --body` "Unsupported Media Type" | Inline JSON strings get mangled by PowerShell. Always use `@$tmpFile` pattern: write body to temp file, pass `--body "@$tmpFile"` |
| aca CLI install 404 | GitHub releases URL requires auth. Use `gh release download` (needs `gh auth login`) or ask user for the .tgz path |
