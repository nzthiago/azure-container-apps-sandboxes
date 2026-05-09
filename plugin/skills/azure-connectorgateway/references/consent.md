# OAuth Consent Flow

How to generate consent links and authenticate connections.

## Generate consent link

```powershell
# Get the connection's objectId and tenantId first
$conn = az rest --method GET `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}?api-version=2026-05-01-preview" | ConvertFrom-Json
$objectId = $conn.properties.createdBy.name
$tenantId = $conn.properties.createdBy.tenantId

# Build consent body — EXACT format required (parameters array)
$body = @{
  parameters = @(@{
    objectId = $objectId
    tenantId = $tenantId
    redirectUrl = "https://microsoft.com"
    parameterName = "token"
  })
} | ConvertTo-Json -Depth 3 -Compress

# Post and open in browser
$tmpFile = New-TemporaryFile
Set-Content $tmpFile $body
$link = az rest --method POST `
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections/{conn}/listConsentLinks?api-version=2026-05-01-preview" `
  --body "@$tmpFile" --query "value[0].link" -o tsv
Remove-Item $tmpFile
Start-Process $link
```

## Critical rules

- **ALWAYS use `Start-Process`** to open consent links — URLs are too long to copy
- **Use `"redirectUrl":"https://microsoft.com"`** — default redirect is broken
- **Do NOT retry with different body formats** — if consent fails, it's a service issue
- **Body format is exact:** `{"parameters":[{"objectId":"...","tenantId":"...","redirectUrl":"https://microsoft.com","parameterName":"token"}]}`
- Get `objectId` and `tenantId` from the connection's `properties.createdBy`

## Verify connection status

After user authenticates, verify:
```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/connectorGateways/{gw}/connections?api-version=2026-05-01-preview" \
  --query "value[].{name:name, status:properties.statuses[0].status}"
# All should show: Connected. If not, re-consent.
```
