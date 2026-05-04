---
name: azure-connectorgateway
description: |
  Azure Connector Gateway — manage gateways, connections, triggers, and MCP configs.
  Connects external services (Office 365, GitHub, Azure Blob) to sandbox actions
  via event-driven triggers or MCP tool integrations.
  Use when:
  - Creating or managing connector gateways and connections
  - Creating or managing trigger configs on a connector gateway
  - Subscribing to connector events (email, file, webhook)
  - Wiring event sources to sandbox callbacks
  - Managing trigger lifecycle (enable, disable, delete)
  - Setting up MCP configs for tool integration
  Triggers: "create trigger", "trigger config", "webhook trigger",
  "connector gateway", "mcp config", "connection", "email trigger"
---

# Azure Connector Gateway

Manage connector gateways, connections, triggers, and MCP configs — connect
external services to sandbox actions (run command, call HTTP port, tool integration).

> **⚠️ Do NOT hallucinate CLI commands or API calls.**
> Use `--help` on any command. Reference files in `references/` for details.

> **⚠️ Do NOT generate a Jupyter notebook, standalone script, or one-shot automation.**
> Walk the user through setup **interactively** — ask questions, execute each step,
> and wait for confirmation before proceeding.

## Interactive Flow (FOLLOW THIS)

When a user asks to create a trigger, set up event-driven automation, or connect
an external service to a sandbox, **guide them interactively step by step**.
Do NOT skip to generating code or notebooks.

### Step 1: Understand the scenario
Ask the user:
- "What event do you want to trigger on?" (new email, SharePoint list item, file upload, etc.)
- Map the answer to a connector: `office365`, `sharepointonline`, `onedriveforbusiness`, etc.
- Ask if they already know the trigger operation, or want to discover available ones.

**Stop and wait for the user's answer before continuing.**

### Step 2: Gateway setup
Ask the user:
- "Do you have an existing connector gateway, or should I create a new one?"
- If **existing**: ask for resource group + gateway name, then retrieve it:
  ```python
  gw = conn_client.get_gateway(gateway_name)
  gw_principal_id = gw['identity']['principalId']
  gw_tenant_id = gw['identity']['tenantId']
  ```
- If **new**: ask for resource group + gateway name + location, then create it
  with a **SystemAssigned managed identity** (required for trigger callbacks):
  ```python
  gw = conn_client.create_gateway(gateway_name, location=location,
      identity={'type': 'SystemAssigned'})
  gw_principal_id = gw['identity']['principalId']
  gw_tenant_id = gw['identity']['tenantId']
  ```
- **Always** capture `principalId` and `tenantId` — they are needed later for
  access policies and InvokePort auth.

**Stop and wait for the user's answer before continuing.**

### Step 3: Create connection + authenticate
- Create an OAuth connection on the gateway for the chosen connector:
  ```python
  conn_client.create_connection(gateway_name, connection_name,
      connector_name='office365')
  ```
- Generate and show the consent URL:
  ```python
  link = conn_client.generate_consent_link(gateway_name, connection_name)
  print(f"Please authenticate here: {link}")
  ```
- **Wait for the user to confirm** they have completed authentication.
- Then verify:
  ```python
  conn = conn_client.get_connection(gateway_name, connection_name)
  status = conn['properties']['statuses'][0]['status']
  # Should be 'Connected'
  ```

### Step 4: Choose integration pattern
Ask the user:
- "How do you want to use this connection?"
  - **A) Direct API calls** — your app calls connector operations on demand
    (e.g., send email, read SharePoint list, create OneDrive file)
  - **B) Event-driven triggers** — the gateway pushes notifications to your
    sandbox when events happen (e.g., new email arrives, list item created)

**Stop and wait for the user's answer before continuing.**

- If **A (Direct API calls)** → go to **Step 5A**
- If **B (Event-driven triggers)** → go to **Step 5B**

---

### Step 5A: Direct API calls via connection runtime URL

The connection's `connectionRuntimeUrl` is a gateway-managed endpoint. Your app
calls it **directly** with the operation's HTTP method, path, and parameters —
the gateway injects the stored OAuth credentials and forwards to the connector.
No trigger config needed.

1. Get the connection runtime URL:
   ```python
   conn = conn_client.get_connection(gateway_name, connection_name)
   runtime_url = conn['properties']['connectionRuntimeUrl']
   print(f"Runtime URL: {runtime_url}")
   ```

2. Discover available operations — get the connector's Swagger to find operation
   paths, methods, and parameters:
   ```python
   ops = conn_client.list_operations(gateway_name, connector_name)
   for op in ops:
       print(f"  • {op['operationId']}: {op.get('summary', '')}  [{op.get('method','?').upper()} {op.get('path','')}]")
   ```
   Present the operations to the user. Common ones:
   - Office 365: `SendMailV2`, `GetEmails`, `GetEvents`
   - SharePoint: `GetItems`, `PostItem`, `PatchItem`
   - OneDrive: `CreateFile`, `GetFileContent`, `ListFolder`

3. **Call the connection runtime URL directly.** The URL format matches how
   AIGatewayRuntimeEngine calls connectors:

   ```
   {method} {connectionRuntimeUrl}/{path}?{queries}

   Headers:
     Authorization: Bearer {access_token}
     Content-Type: application/json
   ```

   Build the request payload from the Swagger operation definition:
   - **Path params** (`in: path`) → substituted into the URL path
   - **Query params** (`in: query`) → appended as query string
   - **Body params** (`in: body`) → sent as JSON request body
   - **Header params** (`in: header`) → added as HTTP headers

   Example using the `request` format via `invoke_dynamic`:
   ```python
   # Option A: By operation ID (gateway resolves path/method from Swagger)
   result = conn_client.invoke_dynamic(gateway_name, connection_name,
       operation_id="SendMailV2",
       parameters={
           "emailMessage": {
               "To": "newhire@contoso.com",
               "Subject": "Welcome!",
               "Body": "<p>Welcome to the team!</p>",
           }
       })

   # Option B: By raw HTTP method + path (matches runtime URL pattern directly)
   result = conn_client.invoke_dynamic(gateway_name, connection_name,
       method="POST", path="/v2/Mail")
   ```

   Or call the runtime URL directly with `httpx`/`requests` from inside
   a sandbox (matching AIGatewayRuntimeEngine payload format):
   ```python
   import httpx
   from azure.identity import ManagedIdentityCredential

   credential = ManagedIdentityCredential()
   token = credential.get_token("https://apihub.azure.com/.default").token

   # URL = {connectionRuntimeUrl}/{operationPath}
   url = f"{runtime_url}/v2/Mail/send"

   response = httpx.post(url,
       headers={
           "Authorization": f"Bearer {token}",
           "Content-Type": "application/json",
       },
       json={
           "emailMessage": {
               "To": "newhire@contoso.com",
               "Subject": "Welcome!",
               "Body": "<p>Welcome to the team!</p>",
           }
       })
   print(response.status_code, response.json())
   ```

4. If running from a **sandbox**, configure egress and access policy:
   ```python
   # Egress policy must allow: *.connectorgateway.azure.com, login.microsoftonline.com

   # Grant sandbox managed identity access to the connection
   conn_client.create_access_policy(gateway_name, connection_name,
       principal_id=sandbox_principal_id,
       tenant_id=sandbox_tenant_id,
       location=gateway_location)
   ```

**→ Skip to Final verification checklist (Direct API).**

---

### Step 5B: Discover trigger operations
- List available trigger operations for the connector:
  ```python
  ops = trigger_client.list_trigger_operations(gateway_name, 'office365')
  for op in ops:
      print(f"  • {op['operationId']}: {op.get('summary', '')}")
  ```
- Present the operations to the user as choices (show summary + operationId).
- Let the user pick which trigger operation to use.

**Stop and wait for the user's selection before continuing.**

### Step 6B: Collect trigger parameters
- Based on the selected operation, ask the user for required parameters.
- Common examples:
  - Email trigger: `folderPath` (Inbox), `subjectFilter` (optional)
  - SharePoint trigger: `siteUrl`, `listName`
  - OneDrive trigger: `folderPath`
- Build the parameters list:
  ```python
  parameters = [
      {'name': 'folderPath', 'value': 'Inbox'},
      {'name': 'subjectFilter', 'value': 'Feedback'},
  ]
  ```

**Stop and wait for the user's answers before continuing.**

### Step 7B: Sandbox target (triggers)
Ask the user:
- "Do you have an existing sandbox, or should I create a new one?"
- If **existing**: ask for sandbox ID + sandbox group name.
- If **new**: create a sandbox group and sandbox. **Important:** after creating the
  sandbox group via ARM, the data plane needs time to register it. Use a retry loop
  with backoff when calling `create_sandbox`:
  ```python
  group = mgmt.create_group(sandbox_group_name, location=location)
  for attempt in range(6):
      try:
          sbx = sbx_client.create_sandbox(sandbox_group_name, disk='ubuntu')
          sandbox_id = sbx['id']
          break
      except Exception as e:
          if attempt < 5 and 'SandboxGroupNotFound' in str(e):
              wait = (attempt + 1) * 10
              print(f'Waiting {wait}s for sandbox group to propagate...')
              time.sleep(wait)
          else:
              raise
  ```
  Then wait for Running state.
- Ask for the **callback type**:
  - **ShellCommand** — run a shell command when the trigger fires (e.g., `python /app/handler.py`)
  - **ExecuteCommand** — run a command directly without a shell (e.g., `python` with args)
  - **InvokePort** — POST to an HTTP port on the sandbox (e.g., port 5000, path `/webhook`)

**Stop and wait for the user's selection before continuing.**

### Step 8B: Create trigger config + access policy
- Create the trigger config:
  ```python
  # For InvokePort target:
  trigger = trigger_client.create_trigger(gateway_name, trigger_config_name,
      connector_name='office365',
      connection_name=connection_name,
      operation_name='OnNewEmailV3',
      sandbox_id=sandbox_id,
      sandbox_group=sandbox_group_name,
      port=5000, port_path='/webhook', http_method='POST',
      parameters=parameters)

  # For ShellCommand target:
  trigger = trigger_client.create_trigger(gateway_name, trigger_config_name,
      connector_name='office365',
      connection_name=connection_name,
      operation_name='OnNewEmailV3',
      sandbox_id=sandbox_id,
      sandbox_group=sandbox_group_name,
      command='python /app/handler.py',
      parameters=parameters)
  ```
- Create the access policy granting the gateway MI access to the connection:
  ```python
  conn_client.create_access_policy(gateway_name, connection_name,
      principal_id=gw_principal_id,
      tenant_id=gw_tenant_id,
      location=gateway_location)
  ```
- **If InvokePort**: also configure port auth so the gateway can call the sandbox port:
  ```python
  sbx_client.add_port(sandbox_id, sandbox_group_name, 5000,
      entra_id_object_ids=[gw_principal_id])
  ```

### Step 9B: Verify trigger is active
- Check the trigger state:
  ```python
  tc = trigger_client.get_trigger(gateway_name, trigger_config_name)
  state = tc['properties']['state']
  # Should be 'Enabled'
  ```
- If not enabled, wait a moment and re-check.

### Final verification checklist

**For Direct API calls (path A):**
- ✅ Gateway exists
- ✅ Connection exists and status is `Connected`
- ✅ `connectionRuntimeUrl` is available (not empty)
- ✅ Access policy exists (sandbox MI → connection) if running from sandbox
- ✅ Sandbox egress allows `*.connectorgateway.azure.com` and `login.microsoftonline.com`
- ✅ Test call to runtime URL returns expected data

**For Event-driven triggers (path B):**
- ✅ Gateway exists with SystemAssigned identity
- ✅ Connection exists and status is `Connected`
- ✅ Trigger config exists and state is `Enabled`
- ✅ Access policy exists (gateway MI → connection)
- ✅ Sandbox is Running (for InvokePort targets)
- ✅ Port auth is configured (for InvokePort targets — gateway principalId in objectIds)

**IMPORTANT: Do NOT skip to code generation. Walk the user through each step interactively.**

## Install

### As plugin (coding agents)
```bash
# Copilot CLI
/plugin marketplace add Azure-Samples/azure-container-apps-sandboxes
/plugin install azure-connectorgateway@Azure-Container-Apps

# Claude Code
claude plugin add Azure-Samples/azure-container-apps-sandboxes
```

### az CLI extension
```bash
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "az_cli_connectorgateway-*-py3-none-any.whl" --dir /tmp
az extension add --source /tmp/az_cli_connectorgateway-*-py3-none-any.whl
```

### Python SDK
```bash
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_connectorgateway-*-py3-none-any.whl" --dir /tmp
pip install /tmp/azure_connectorgateway-*-py3-none-any.whl
```

### Uninstall
```bash
az extension remove --name trigger
pip uninstall azure-connectorgateway
```

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Connector Gateway (ARM: Microsoft.Web/connectorGateways)     │
│                                                               │
│  ├── Connections (OAuth-authorized connector access)          │
│  ├── Trigger Configs (event subscription → callback)          │
│  └── Access Policies (MI → connection access)                 │
│                                                               │
│  Trigger Config subscribes to connector events and            │
│  POSTs notifications to the sandbox callback URL              │
└──────────────────────────────────────────────────────────────┘
           │                                │
           │ connector event fires          │ callback to sandbox
           ▼                                ▼
┌─────────────────────┐    ┌──────────────────────────────────┐
│  Connector           │    │  Sandbox (KVM µVM)                │
│  (Office 365, GitHub │    │  executeShellCommand (command)    │
│   Blob Storage, etc) │    │  or proxy port (InvokePort)       │
└─────────────────────┘    └──────────────────────────────────┘
```

## Python SDK

```python
from azure.connectorgateway import ConnectorGatewayClient, TriggerClient
from azure.sandbox import SandboxClient

conn_client = ConnectorGatewayClient(resource_group="my-rg")
trigger_client = TriggerClient(resource_group="my-rg")
sbx_client = SandboxClient(resource_group="my-rg")

# Create trigger with ShellCommand target (shell-interpreted command)
trigger = trigger_client.create_trigger("my-gw", "email-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id="my-sandbox-id",
    sandbox_group="my-sg",
    command="python /app/handle_email.py",
    parameters=[{"name": "folderPath", "value": "Inbox"}])

# Create trigger with ExecuteCommand target (direct exec, no shell)
trigger = trigger_client.create_trigger("my-gw", "cmd-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id="my-sandbox-id",
    sandbox_group="my-sg",
    execute_command="python",
    execute_args=["/app/handle_email.py", "--verbose"],
    parameters=[{"name": "folderPath", "value": "Inbox"}])

# Create trigger with InvokePort target (HTTP call to sandbox port)
trigger = trigger_client.create_trigger("my-gw", "webhook-handler",
    connector_name="office365",
    connection_name="o365-conn",
    operation_name="OnNewEmailV3",
    sandbox_id="my-sandbox-id",
    sandbox_group="my-sg",
    port=5000,
    port_path="/webhook",
    parameters=[{"name": "folderPath", "value": "Inbox"}])

# Lifecycle
trigger_client.disable_trigger("my-gw", "email-handler")
trigger_client.enable_trigger("my-gw", "email-handler")
trigger_client.delete_trigger("my-gw", "email-handler")
```

Run `help(trigger_client)` to see all available methods.

## az CLI

```bash
az connectorgateway trigger list -g my-rg --gateway my-gw
az connectorgateway trigger create -g my-rg --gateway my-gw -n email-handler \
  --connector-name office365 --connection-name o365-conn \
  --operation-name OnNewEmail \
  --sandbox-id my-sandbox-id -s my-sandbox-group \
  --port 5000 --port-path /webhook
az connectorgateway trigger enable -g my-rg --gateway my-gw -n email-handler
az connectorgateway trigger disable -g my-rg --gateway my-gw -n email-handler
az connectorgateway trigger operations list -g my-rg --gateway my-gw --connector-type office365
az connectorgateway trigger delete -g my-rg --gateway my-gw -n email-handler
```

Run `az connectorgateway --help` to see all available commands.

## Key Concepts

| Concept | What it is |
|---------|-----------|
| **Trigger Config** | ARM resource on the gateway (`connectorGateways/{gw}/triggerConfigs/{name}`). Subscribes to connector events and delivers to sandbox callback URL |
| **Callback URL** | ADC sandbox endpoint or proxy port URL — built automatically by the SDK |
| **Access Policy** | Grants the gateway MI access to the connection (required for event subscription) |
| **ShellCommand target** | Callback URL = `{ADC}/.../{sandboxId}/executeShellCommand`; body = `{command, shell, ...}`. Shell-interpreted command string. |
| **ExecuteCommand target** | Callback URL = `{ADC}/.../{sandboxId}/executeCommand`; body = `{command, args, ...}`. Direct exec, no shell. |
| **InvokePort target** | Callback URL = `https://{sandboxId}--{port}.proxy.azuredevcompute.io/...`. HTTP call to sandbox port. |

## Trigger Operations

| Type | How it works | Example |
|------|-------------|---------|
| **batch** | Periodic polling check | "When a file is created or modified" |
| **single** | Real-time notification | "When a new email arrives" |

## Gotchas

| Issue | Solution |
|-------|----------|
| Trigger not firing | Ensure access policy exists granting gateway MI access to the connection |
| Gateway can't subscribe | Create an access policy granting the gateway MI access to the connection |
| Sandbox must be Running | For InvokePort targets, sandbox must be running; for ShellCommand, sandbox activates on demand |
| Port auth for InvokePort | Add gateway's principalId to the port's entraId objectIds on the sandbox |
| Cleanup order | Delete trigger config → connection → sandbox → gateway |
| SandboxGroupNotFound 404 | Data plane needs time after ARM sandbox group creation. Retry `create_sandbox` with backoff (10-60s waits, up to 6 attempts) |

## Labs

See [labs/README.md](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/README.md) for trigger labs.

## References

- [prerequisites.md](references/prerequisites.md)
- [quickstart.md](references/quickstart.md)
- [trigger-flow.md](references/trigger-flow.md)
