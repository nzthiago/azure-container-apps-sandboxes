# Triage new Outlook emails into Teams

A new email arrives in your Outlook inbox. This sample looks at each
one, decides whether it actually matters (a paged incident, an
urgent customer request, that kind of thing), and if it does, posts
a short triage card into your Teams channel with the sender, the
subject, and one sentence on why it matters.

How it works in one paragraph. A Connector Namespaces trigger fires
on the new email and POSTs to a small ACA receiver app. The receiver
boots a fresh sandbox for that email, drops a triage prompt inside,
and runs GitHub Copilot CLI. Copilot reads the email, makes the
call, and (when the email is important) uses the Teams MCP server to
post the card. The receiver holds the MCP API key, but the sandbox's
egress proxy stamps it on outbound calls at the boundary, so the
sandbox process itself never sees a credential.

## Deploy and test

You'll need `azd`, the `az` CLI, Docker (for `azd deploy receiver`),
an M365 mailbox you can sign in to, and a Teams channel you can post
into.

```bash
azd auth login
az login

azd env set ACA_SANDBOX_REGION westus2   # any region where ACA sandboxes are available
azd env set GITHUB_PAT          <ghp_…>  # for Copilot CLI to call GitHub Models

azd up
```

The post-deploy script does a few things. It fetches the MCP runtime
API key from the namespace and stores it as a Container App secret
on the receiver. It opens two browser tabs so you can sign in to the
two connections (`office365` for the trigger, `a365teamsmcp` for the
Teams MCP). Once both connections show `Connected`, send yourself a
test email. The receiver picks it up, boots a sandbox, and a triage
card lands in your configured Teams channel.

To watch the receiver process emails:

```bash
az containerapp logs show \
  -g "$(azd env get-value AZURE_RESOURCE_GROUP)" \
  -n "$(azd env get-value RECEIVER_CONTAINER_APP_NAME)" --follow
```

## Clean up

While this sample is deployed, every new email in the consented
mailbox wakes a sandbox and runs Copilot CLI against it (which
consumes GitHub Models tokens). Fine for a hands-on demo, but you
probably don't want this running indefinitely against your real
inbox. Tear it down when you're done:

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
    │  POST /webhook  (system-key today; Entra MI in production)
    ▼
receiver  (ACA Container App, FastAPI)
    │  acks 202, then per email:
    │    1. begin_create_sandbox + install Copilot CLI
    │    2. set_egress_default(Deny) + Transform: X-API-Key on MCP host
    │    3. write prompt.md and ~/.copilot/mcp-config.json
    │    4. copilot --allow-all-tools -p prompt.md
    │    5. delete sandbox
    ▼
sandbox egress proxy
    │  stamps X-API-Key on outbound MCP calls
    │  (sandbox process never sees the key)
    ▼
Teams MCP (a365teamsmcp via Connector Namespace)
    │  SendMessageToChannel(teamId, channelId, content)
    ▼
Teams channel: triage card delivered
```

## Security model

| Component | Role |
|---|---|
| `office365` connection | OAuth to your mailbox. Used only by the trigger. |
| `a365teamsmcp` connection | OAuth to your Teams account. Used only by the MCP backend the sandbox calls. |
| mcpserverConfig (`kind: ManagedMcpServer`) | Namespace-published MCP HTTP endpoint. Authenticates with `X-API-Key` (stamped by the egress proxy). |
| Receiver Container App | Holds the MCP API key as a secret (env via `secretref`). Boots the sandbox per email and applies its egress policy. |
| Sandbox egress proxy | Deny default plus a Transform rule that stamps `X-API-Key` on the MCP host. The sandbox process never sees the key. |

The receiver authenticates incoming webhooks (a shared secret today;
swap for App Service built-in auth plus a Connector Namespace MI in
production). The sandbox holds **no MCP credential**. The receiver
issues a Transform rule on the egress proxy that injects the
`X-API-Key` header at the boundary, so Copilot CLI sees only the
bare MCP URL.

## Going further

A few tips when you take this past a demo.

- Pre-bake the sandbox disk so per-email cold start drops from tens
  of seconds (install Copilot, install OS deps) to seconds.
- Replace the shared-secret webhook with an Entra-validated path.
  Put `Microsoft.App/containerApps/authConfigs` in front of the
  receiver and restrict `allowedPrincipals.identities` to the
  Connector Namespace's MI principalId.
- Label sandboxes by message-id so a janitor process can reap
  stragglers if the receiver crashes mid-run.

Today both connections are owned by whoever ran the OAuth consent.
Multi-tenant deployments need `connections/accessPolicies` and
per-tenant secrets, which is out of scope for this sample.

> Connector Namespaces (`Microsoft.Web/connectorGateways`) is in
> preview. Bicep emits `BCP081` schema-cache warnings that don't
> block deployment. Expect breaking changes between preview
> milestones.