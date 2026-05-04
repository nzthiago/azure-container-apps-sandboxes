# Lab 04 – Email Summarizer

AI-powered email summarizer: new emails trigger automatic summarization via
Azure OpenAI, with summaries saved to OneDrive and notifications posted to Teams.

```
New Email → Office 365 Trigger → Gateway → Sandbox (Flask :5000)
                                              ├── Azure OpenAI → summarize
                                              ├── OneDrive → save .md summary
                                              └── Teams webhook → notify
```

## What You'll Build

1. **Office 365 email trigger** — fires when a new email arrives in your Inbox
2. **Email summarizer** — Flask app in a sandbox that:
   - Receives the email payload via webhook
   - Summarizes the content using Azure OpenAI (GPT-4o)
   - Saves a markdown summary to OneDrive (`/EmailSummaries/{date}-{subject}.md`)
   - Posts a notification card to a Teams channel
   - Displays a dashboard with all processed emails and summaries

## Prerequisites

- Azure CLI [signed in](https://learn.microsoft.com/cli/azure/authenticate-azure-cli-interactively)
- SDKs: `pip install azure-connectorgateway azure-sandbox azure-mgmt-sandbox`
- **Azure OpenAI** resource with a deployed model (e.g., `gpt-4o`)
- **Teams incoming webhook URL** (optional — for Teams notifications)

## Setup

This lab uses an **interactive setup flow**. Run the setup script and follow
the prompts — it will walk you through each step:

```bash
python setup.py
```

The setup script will:
1. Create a resource group and connector gateway
2. Create an Office 365 connection (you'll need to click an OAuth consent link)
3. Create a sandbox and deploy the Flask app
4. Ask for your Azure OpenAI endpoint, key, and deployment name
5. Optionally ask for a Teams webhook URL
6. Create the email trigger and verify it's active

## Testing

Once setup is complete:
1. Send yourself an email
2. Wait 30–60 seconds for the trigger to fire
3. Visit the dashboard URL (printed during setup) to see the summary

## Architecture Details

| Component | Purpose |
|-----------|---------|
| Connector Gateway | Manages OAuth connections and trigger subscriptions |
| Office 365 Connection | Authorized access to your Outlook mailbox |
| Trigger Config | Subscribes to `OnNewEmailV3` and POSTs to sandbox |
| Sandbox (Flask) | Receives email payloads, runs the summarization pipeline |
| Azure OpenAI | Generates 2-3 sentence summaries of email content |
| OneDrive (Graph API) | Stores markdown summaries in `/EmailSummaries/` |
| Teams Webhook | Posts notification cards to a channel |

## Graceful Degradation

The app works even if not all services are configured:
- **No Azure OpenAI** → emails are logged with a preview instead of a summary
- **No OneDrive token** → summaries are not saved to OneDrive
- **No Teams webhook** → no Teams notifications are sent

The dashboard always shows all processed emails regardless of which services
are configured.

## Cleanup

```bash
python setup.py --cleanup
```

Or manually:
```bash
az group delete --name lab-04-email-summarizer --yes --no-wait
```
