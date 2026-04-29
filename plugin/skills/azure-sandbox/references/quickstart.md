# Quick Start

```bash
# 1. Login
az login

# 2. Create sandbox group
az sandboxgroup create -g my-rg -n my-group -l westus2

# 3. Create sandbox
az sandbox create -g my-rg -s my-group --disk ubuntu

# 4. Run a command
az sandbox exec -g my-rg -s my-group --id <sandbox-id> -c "echo hello"

# 5. SSH in (Node.js recommended, Python also works)
node plugin/skills/azure-sandbox/assets/ssh.mjs <sandbox-id> -g my-rg -s my-group
python scripts/ssh.py <sandbox-id> -g my-rg -s my-group

# 6. Clean up
az sandbox delete -g my-rg -s my-group --id <sandbox-id> --yes
az sandboxgroup delete -g my-rg -n my-group --yes
```

Or open the lab notebook: `labs/01-sandbox-getting-started/01-getting-started.ipynb`
