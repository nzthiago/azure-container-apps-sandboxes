# Quick Start

```bash
# 1. Login
az login

# 2. Create sandbox group
aca sandboxgroup create --name my-group --location westus2 -g my-rg

# 3. Create sandbox
aca sandbox create --disk ubuntu -g my-rg --group my-group

# 4. Run a command
aca sandbox exec --id <sandbox-id> -c "echo hello" -g my-rg --group my-group

# 5. Interactive shell
aca sandbox shell --id <sandbox-id> -g my-rg --group my-group

# 6. Clean up
aca sandbox delete --id <sandbox-id> --yes -g my-rg --group my-group
aca sandboxgroup delete --name my-group --yes -g my-rg
```

Or open the lab notebook: `labs/01-sandbox-getting-started/01-getting-started.ipynb`
