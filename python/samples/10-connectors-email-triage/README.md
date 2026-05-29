# ACA Sandbox: email-triggered Teams notifications

> **A new Outlook email fires a Connector Namespaces trigger → ACA
> receiver app boots a fresh sandbox per email → GitHub Copilot CLI
> in the sandbox classifies the email and (if important) posts a
> triage card to a Teams channel via the Work IQ Teams MCP. The
> receiver holds the MCP API key; the sandbox egress proxy stamps it
> on outbound MCP calls at the boundary, so the sandbox itself never
> sees a credential.**

## Deploy and test

**Prereqs:** `azd`, `az` CLI, Docker (for `azd deploy receiver`), an
M365 mailbox you can authorize against, and a Teams channel you can
post to.

```bash
azd auth login
az login

azd env set ACA_SANDBOX_REGION westus2   # any region where ACA sandboxes are available
azd env set GITHUB_PAT          <ghp_…>  # for Copilot CLI → GitHub Models

azd up
```

The post-deploy hook fetches the MCP runtime API key, stamps it as
a Container App secret on the receiver, and opens **two** browser
tabs for OAuth consent (`office365` for the trigger, `a365teamsmcp`
for the Teams MCP). After both connections show `Connected`, send
yourself a test email — the receiver picks it up, boots a sandbox,
and a triage card lands in your configured Teams channel.

Watch the receiver logs:

```bash
az containerapp logs show \
  -g "$(azd env get-value AZURE_RESOURCE_GROUP)" \
  -n "$(azd env get-value RECEIVER_CONTAINER_APP_NAME)" --follow
```

## Clean up

⚠️ While deployed, every new email in the consented mailbox wakes a
sandbox and runs Copilot CLI against it (consuming GitHub Models
tokens). Fine for a hands-on demo, not for your real inbox. Tear
down when done:

```bash
azd down --purge --force
```

---

## How it works

```
new email in Outlook mailbox
    │  (Connector Namespace polls / webhook-subscribes)
    ▼
triggerConfig (On_new_email_V3)
    │  POST /webhook  (system-key or Entra MI in production)
    ▼
receiver  (ACA Container App, FastAPI)
    │  acks 202, then per email:
    │    1. begin_create_sandbox + install Copilot CLI
    │    2. set_egress_default(Deny) + Transform: X-API-Key on MCP host
    │    3. write prompt.md + ~/.copilot/mcp-config.json
    │    4. copilot --allow-all-tools -p prompt.md
    │    5. delete sandbox
    ▼
sandbox egress proxy
    │  stamps X-API-Key on outbound MCP calls
    │  (sandbox never sees the key)
    ▼
Teams MCP (a365teamsmcp via Connector Namespace)
    │  SendMessageToChannel(teamId, channelId, content)
    ▼
Teams channel — triage card delivered
```

## Security model

| Component | Role |
|---|---|
| `office365` connection | OAuth → your mailbox. Used only by the trigger. |
| `a365teamsmcp` connection | OAuth → your Teams account. Used only by the MCP backend the sandbox calls. |
| mcpserverConfig (`kind: ManagedMcpServer`) | Namespace-published MCP HTTP endpoint. Authenticates with `X-API-Key` (stamped by egress proxy). |
| Receiver Container App | Holds the MCP API key as a secret (env via `secretref`). Boots the sandbox per email and applies its egress policy. |
| Sandbox egress proxy | Deny default + Transform rule that stamps `X-API-Key` on the MCP host. Sandbox process never sees the key. |

The receiver authenticates incoming webhooks (a shared secret today;
swap for App Service built-in auth + Connector Namespace MI for
production). The sandbox holds **no MCP credential** — the receiver
issues a Transform rule on the egress proxy that injects the
`X-API-Key` header at the boundary, so Copilot CLI sees only the
bare MCP URL.

## Going further

- **Pre-bake the sandbox disk** so per-email cold start drops from
  ~tens of seconds (install Copilot, install OS deps) to seconds.
- **Replace shared-secret webhook auth with Entra-validated MI**.
  Put `Microsoft.App/containerApps/authConfigs` in front of the
  receiver and restrict `allowedPrincipals.identities` to the
  Connector Namespace's MI principalId. The
  [`functions-connectors-net-builtinauth`](https://github.com/Azure-Samples/functions-connectors-net-builtinauth)
  sample shows the equivalent pattern on Functions.
- **Label sandboxes by message-id** so a janitor can reap stragglers
  if the receiver crashes mid-run.

Today both connections are owned by whoever ran the OAuth consent.
Multi-tenant deployments require `connections/accessPolicies` and
per-tenant secrets — out of scope here.

> Connector Namespaces (`Microsoft.Web/connectorGateways`) is in
> preview; Bicep emits `BCP081` schema-cache warnings that don't
> block deployment. Expect breaking changes between preview
> milestones.