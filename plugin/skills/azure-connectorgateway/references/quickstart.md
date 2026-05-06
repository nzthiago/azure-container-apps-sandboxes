# Quick Start

```bash
# 1. Login
az login
az account show --query "{subscription:id, tenant:tenantId}" -o table

# 2. List gateways
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways?api-version=2026-05-01-preview" \
  --query "value[].{name:name, location:location}" -o table

# 3. List triggers on a gateway
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/triggerConfigs?api-version=2026-05-01-preview" \
  --query "value[].{name:name, state:properties.state, connector:properties.connectionDetails.connectorName}" -o table

# 4. Discover operations for a connector (classic locations endpoint)
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Web/locations/{location}/managedApis/office365/apiOperations?api-version=2016-06-01"
```

Or open the lab notebook: `labs/02-trigger-getting-started/01-trigger-getting-started.ipynb`
