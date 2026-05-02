# Copilot CLI in a Sandbox (BYOK)

Create a sandbox, install GitHub Copilot CLI, and configure it to use Azure OpenAI via BYOK. Supports zero-trust mode where the API key is injected via egress transform rules so it never enters the sandbox.

> **Agent instructions:** Before running this runbook, check prerequisites (see SKILL.md Prerequisites Check). Then ask the user if they want to run all steps automatically or step through them one at a time. For zero-trust mode (step 3b), ask the user for their Azure OpenAI endpoint, model name, and API key before proceeding.

## Prerequisites

```bash
az login
npm install -g https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases/download/v0.1.0b1/azure-aca-cli-1.0.0-beta.1.tgz
```

> 💡 Using `--cpu 2000m --memory 4096Mi` allocates 2 vCPUs and 4GB RAM — more resources for running Copilot CLI and its LLM interactions. You only pay for what you allocate, per second.

## 1. Create resources

```bash
az group create --name sandbox-lab-rg --location westus2 -o none
aca sandboxgroup create --name sandbox-lab-sg --location westus2 -g sandbox-lab-rg
aca sandbox create --disk ubuntu --cpu 2000m --memory 4096Mi --wait -g sandbox-lab-rg --group sandbox-lab-sg -o json
```

Save the sandbox ID:

```bash
SANDBOX_ID=<id-from-output>
```

> 💡 **Exec** installs software inside the sandbox at runtime. Anything installed persists on the sandbox's disk and survives stop/resume cycles — no need to rebuild images.

## 2. Install Copilot CLI

```bash
aca sandbox exec --id $SANDBOX_ID -c "curl -fsSL https://gh.io/copilot-install | bash 2>&1 | tail -2" -g sandbox-lab-rg --group sandbox-lab-sg
```

> 💡 **BYOK (Bring Your Own Key)** lets you use your own Azure OpenAI endpoint. In standard mode, the API key is set as an environment variable inside the sandbox.

## 3a. Configure BYOK (standard)

Set environment variables inside the sandbox and run Copilot:

```bash
aca sandbox shell --id $SANDBOX_ID -g sandbox-lab-rg --group sandbox-lab-sg
```

Once inside the sandbox shell:

```bash
export COPILOT_PROVIDER_BASE_URL=https://<resource>.openai.azure.com/openai/deployments/<deployment>
export COPILOT_PROVIDER_TYPE=azure
export COPILOT_PROVIDER_API_KEY=<your-api-key>
export COPILOT_MODEL=<model-name>
export COPILOT_OFFLINE=true
copilot
```

> 💡 **Zero-trust egress** is the most secure pattern. The API key never enters the sandbox — instead, the sandbox platform injects it into outbound requests via header transform rules. Even if the sandbox is compromised, the key can't be extracted.

## 3b. Configure BYOK (zero-trust)

In zero-trust mode, the API key is injected via egress transform rules — the key never enters the sandbox.

Create a policy file:

```bash
cat > /tmp/egress-policy.yaml << 'EOF'
defaultAction: Deny

hostRules:
  - pattern: "*.openai.azure.com"
    action: Allow

rules:
  - name: aoai-key-swap
    match:
      host: "<resource>.openai.azure.com"
    action:
      type: Transform
      headers:
        - operation: Set
          name: api-key
          value: "<your-api-key>"
EOF
```

Apply the policy:

```bash
aca sandbox egress apply --id $SANDBOX_ID --file /tmp/egress-policy.yaml -g sandbox-lab-rg --group sandbox-lab-sg
```

Then connect and run Copilot with a placeholder key:

```bash
aca sandbox shell --id $SANDBOX_ID -g sandbox-lab-rg --group sandbox-lab-sg
```

Inside the sandbox:

```bash
export COPILOT_PROVIDER_BASE_URL=https://<resource>.openai.azure.com/openai/deployments/<deployment>
export COPILOT_PROVIDER_TYPE=azure
export COPILOT_PROVIDER_API_KEY=PLACEHOLDER_EGRESS_WILL_SWAP
export COPILOT_MODEL=<model-name>
export COPILOT_OFFLINE=true
copilot
```

> 💡 **One-shot exec** is useful for automation — run a single Copilot prompt and get the result without opening an interactive shell.

## 4. One-shot mode

Run a single prompt without entering the shell:

```bash
aca sandbox exec --id $SANDBOX_ID -c "export COPILOT_PROVIDER_BASE_URL=<url> && export COPILOT_PROVIDER_TYPE=azure && export COPILOT_PROVIDER_API_KEY=<key> && export COPILOT_MODEL=<model> && export COPILOT_OFFLINE=true && copilot -p 'your prompt here' 2>&1" -g sandbox-lab-rg --group sandbox-lab-sg
```

> 💡 You can delete just the sandbox (keeping the group for future sandboxes) or do a full cleanup including the resource group.

## 5. Clean up

Delete sandbox only:

```bash
aca sandbox delete --id $SANDBOX_ID --yes -g sandbox-lab-rg --group sandbox-lab-sg
```

Full cleanup (sandbox group + resource group):

```bash
aca sandboxgroup delete --name sandbox-lab-sg --yes -g sandbox-lab-rg
az group delete --name sandbox-lab-rg --yes --no-wait
```
