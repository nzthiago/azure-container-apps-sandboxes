# Quick Start

```bash
# 1. Login
az login

# 2. Install SDK
gh release download --repo Azure-Samples/azure-container-apps-sandboxes --pattern "azure_trigger-*.whl" --dir /tmp
pip install /tmp/azure_trigger-*.whl

# 3. List triggers
python -c "
from azure.connectorgateway import TriggerClient
c = TriggerClient(resource_group='my-rg')
triggers = c.list_triggers('my-gateway')
print(f'Triggers: {len(triggers)}')
for t in triggers:
    print(f'  {t[\"name\"]}: {t[\"properties\"][\"state\"]}')
"

# 4. Discover operations for a connector
python -c "
from azure.connectorgateway import TriggerClient
c = TriggerClient(resource_group='my-rg')
ops = c.list_trigger_operations('my-gateway', 'office365')
for op in ops:
    print(f'  {op[\"operationId\"]}: {op[\"summary\"]} ({op[\"triggerType\"]})')
"
```

Or open the lab notebook: `labs/02-trigger-getting-started/01-trigger-getting-started.ipynb`
