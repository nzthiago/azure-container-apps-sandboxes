# Connection Runtime URL Examples

How to call connector operations directly from a sandbox using connection runtime URLs.

## URL Format

```
{HTTP_METHOD} {connectionRuntimeUrl}/{operation_path}?{query_params}
Content-Type: application/json
(Authorization is injected by egress — do NOT set it yourself)
```

## How to map Swagger operations to runtime URL calls

1. **Find the operation** from the connector's Swagger (use `GET .../locations/{location}/managedApis/{connector}/apiOperations?api-version=2016-06-01`)

2. **Build the URL**:
   - Base: `connectionRuntimeUrl` (from connection properties)
   - Path: the operation's `path` field from Swagger
   - Query params: append as `?key=value&key2=value2`

3. **Map parameters by location** (from Swagger `in` field):
   | Swagger `in` | Where it goes |
   |--------------|---------------|
   | `path` | Substitute into URL path (e.g., `/teams/{teamId}/channels` → `/teams/abc123/channels`) |
   | `query` | Append as query string: `?folderPath=/&name=test.txt` |
   | `body` | Send as JSON request body |
   | `header` | Add as HTTP header (but NOT `Authorization` — egress handles that) |

## Teams — Post message to channel

```bash
curl -sk -X POST "${RUNTIME_URL}/beta/teams/conversation/message/poster/user/location/Channel" \
  -H "Content-Type: application/json" \
  -d '{
    "recipient": {
      "groupId": "{team_id}",
      "channelId": "{channel_id}"
    },
    "messageBody": "<p>Hello from sandbox!</p>"
  }'
```

## OneDrive — Create file (text)

```bash
curl -sk -X POST "${RUNTIME_URL}/datasets/default/files?folderPath=%2FMyFolder&name=report.txt" \
  -H "Content-Type: application/json" \
  -d '"File content goes here as a JSON string"'
```

## OneDrive — Create file (binary)

```bash
curl -sk -X POST "${RUNTIME_URL}/datasets/default/files?folderPath=%2FMyFolder&name=image.png" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @/path/to/local/file.png
```

## Office 365 — Send email

```bash
# Flat body format — works for both dynamicInvoke AND runtime URL calls
curl -sk -X POST "${RUNTIME_URL}/v2/Mail" \
  -H "Content-Type: application/json" \
  -d '{
    "To": "user@contoso.com",
    "Subject": "Hello from sandbox",
    "Body": "<p>This was sent via the connection runtime URL</p>"
  }'
# NOTE: Do NOT wrap in {"emailMessage": {...}} — that returns 400
```

## Office 365 — Get emails (with attachments)

```bash
# Always use includeAttachments=true to get attachments inline
curl -sk "${RUNTIME_URL}/v2/Mail?folderPath=Inbox&top=5&includeAttachments=true"
# Response includes Attachments[] array with ContentBytes (base64) for each email
```

> **⚠️ Do NOT use separate attachment endpoints** (`/codeless/v1.0/me/messages/{id}/attachments/{id}`
> or `/v2/Mail/{id}/Attachments/{id}`) — they return 404 from runtime URLs.
> Always use `includeAttachments=true` on the `/v2/Mail` query instead.
> Attachments are returned inline as `email.Attachments[].ContentBytes` (base64-encoded).

## SharePoint — Get list items

```bash
curl -sk "${RUNTIME_URL}/datasets/{encoded_site_url}/tables/{list_name}/items"
```

## SharePoint — Create list item

```bash
curl -sk -X POST "${RUNTIME_URL}/datasets/{encoded_site_url}/tables/{list_name}/items" \
  -H "Content-Type: application/json" \
  -d '{"Title": "New item", "Status": "Active"}'
```

## Important notes

- URL-encode path segments and query values (spaces → `%20` or `+`)
- For OneDrive file content, use `Content-Type: application/octet-stream` for binary
  or `application/json` with a JSON string for text content
- The response format varies by connector — some return the created resource,
  some return `{"statusCode": 200}`, some return raw data
- For Teams `messageBody`: HTML is supported (`<p>`, `<b>`, `<a>`, etc.)
- For attachment content: `ContentBytes` is base64-encoded — decode with `base64.b64decode()`
- Attachment endpoints (`/codeless/`, `/v1.0/`) return 404 — always use `includeAttachments=true`
- Strip the `/{connectionId}` prefix from Swagger paths — connection context is already set
