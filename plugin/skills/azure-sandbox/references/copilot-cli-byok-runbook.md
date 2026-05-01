# Copilot CLI in a Sandbox (BYOK)

Create a sandbox, install GitHub Copilot CLI, and configure it to use Azure OpenAI via BYOK. Supports zero-trust mode where the API key is injected via egress transform rules so it never enters the sandbox.

## Prerequisites

```bash
az login
npm install -g @azure/aca-cli
```

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

## 2. Install Copilot CLI

```bash
aca sandbox exec --id $SANDBOX_ID -c "curl -fsSL https://gh.io/copilot-install | bash 2>&1 | tail -2" -g sandbox-lab-rg --group sandbox-lab-sg
```

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

## 4. One-shot mode

Run a single prompt without entering the shell:

```bash
aca sandbox exec --id $SANDBOX_ID -c "export COPILOT_PROVIDER_BASE_URL=<url> && export COPILOT_PROVIDER_TYPE=azure && export COPILOT_PROVIDER_API_KEY=<key> && export COPILOT_MODEL=<model> && export COPILOT_OFFLINE=true && copilot -p 'your prompt here' 2>&1" -g sandbox-lab-rg --group sandbox-lab-sg
```

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
