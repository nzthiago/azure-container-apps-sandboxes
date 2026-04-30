# Trigger Getting Started

Build an event-driven feedback pipeline — incoming emails trigger real-time
sentiment analysis in a sandbox-hosted Flask app.

## What You'll Do

1. Create a connector gateway with managed identity
2. Authorize access to your Outlook mailbox via OAuth
3. Spin up a sandbox running a Flask sentiment analyzer
4. Create a trigger config: new email → POST to dashboard
5. Send an email and watch the live dashboard update
6. Manage trigger lifecycle (disable / enable)
7. Clean up

## How to Run

| Mode | How | Best for |
|------|-----|----------|
| **Notebook** | Open `01-trigger-getting-started.ipynb` in VS Code | Humans — step by step with explanations |
| **Script** | `python plugin/skills/azure-connectorgateway/scripts/trigger-getting-started.py --gateway my-gw` | Agents, CI, quick test |

## Prerequisites

- Azure CLI: `az login`
- SDKs: `pip install azure-connectorgateway azure-sandbox azure-mgmt-sandbox`
