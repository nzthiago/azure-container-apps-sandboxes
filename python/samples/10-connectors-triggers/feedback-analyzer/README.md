# Scenario — email → sandbox → AI-composed reply

Round-trip Office 365 → sandbox → Office 365. A connector trigger
fires on a new "Feedback" email, the sandbox hands the body
to the pre-installed **GitHub Copilot CLI**, and a personalized
acknowledgment is sent back through the **same** Office 365
connection — one OAuth consent, both directions.

## What you get

For every email whose subject contains **Feedback**, you receive a
reply titled `Auto-ack: received your message` with a warm,
AI-composed acknowledgment that references the points you raised.
The prompt template is `_COPILOT_PROMPT_TEMPLATE` in
[`sandbox-app/server.py`](../../../../cli/samples/10-connectors-triggers/feedback-analyzer/sandbox-app/server.py)
— tweak it for persona, length, language, or structured output. (The
`sandbox-app/` name reflects that the listener runs **inside the
sandbox**, not on your machine. It's shared with the CLI flavor of this
sample to avoid duplication; this Python tree's `run.py` uploads it
from the CLI tree.)

## Prerequisites

- The scenario setup applied (`../setup/setup.py`) — provisions the
  connector, the Office 365 connection, and access policies.
- A **GitHub token** for the Copilot CLI. The script picks one up
  automatically from `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` /
  `GITHUB_TOKEN`, then falls back to `gh auth token`, then prompts.
  Accepted: OAuth tokens (`ghu_`, `gho_`) and fine-grained PATs with
  the **Copilot Requests** permission. Classic `ghp_` tokens are
  **not** supported.

Optional: `TRIAGE_RECIPIENT=triage@contoso.com` in `.env`. By default
the acknowledgment goes back to `ACA_USER_EMAIL` (the mailbox you
consented with) — not to the email's original sender. So if you're
testing from the same mailbox, you'll see the reply in your own inbox.

## Run

```bash
pip install -r requirements.txt
python run.py
```

## Notes

- The subject filter is `Feedback`. The reply subject is engineered
  not to contain that word, so you don't get an infinite self-trigger.
- Expect ~1–3 minutes from "send" to "reply arrives" — Office 365
  polls the inbox roughly every 1 minute and Copilot composition adds
  another 30–60s. If the sandbox is suspended, the port's
  `activationMode=OnDemand` causes the proxy to RESUME it before
  forwarding the webhook (adds a few seconds).
