# 07 — Computer use

> Run an LLM-driven *computer-use* agent inside an Azure Container Apps
> sandbox. The agent sees the desktop via screenshots and drives it with
> mouse + keyboard — the same way a human would. The sandbox is ephemeral,
> deny-by-default on egress, isolated from your network and credentials,
> and you watch the whole thing in your browser via noVNC.

## What this unlocks

Computer use is the answer when **the software you need to automate has no
API** — or the API doesn't cover what's behind the UI. The pattern fits:

- **Third-party SaaS** (supplier portals, ad networks, EHRs, government sites)
- **Legacy desktop / internal apps** nobody will rewrite
- **End-to-end UI tests** written in natural language
- **Browser automation that survives redesigns** (vision is robust to layout)
- **Knowledge-worker chores** ("file this in SAP / pull yesterday's report")
- **Cross-app workflows behind SSO** (run the agent inside your VNet)

## Why an ACA sandbox is the right runtime

Both OpenAI and Anthropic's docs tell you the same thing: *"use a dedicated
VM or container with minimal privileges and limit internet access to an
allowlist."* That's a TODO. Sandboxes deliver it as primitives:

| Vendor safety guidance | Sandbox primitive |
|---|---|
| Dedicated VM / container per task | `begin_create_sandbox(...)` — gone when the script exits |
| Limit internet to an allowlist | `set_egress_default("Deny")` + `add_egress_host_rule(...)` |
| No sensitive data on the host | Sandbox boots empty — no creds, no host filesystem, no other tenants |
| Audit + replay | noVNC live view + (optional) `ffmpeg` recording to a blob volume |
| Run many agents in parallel | `asyncio.gather` over N `begin_create_sandbox` calls |

Plus the things sandboxes give you that the vendor docs don't:

- **Boot in seconds** when you bake the desktop into a disk image (guide 03)
- **One public noVNC URL per run** via `add_port(6080, anonymous=True)` — paste it into Chrome
- **Per-sandbox egress policy** that ships with the sandbox, not the app

## What's in this scenario

```
07-computer-use/
├── README.md            (this file)
├── desktop-image/       Shared Linux desktop — Xvfb + Chromium + xdotool +
│                        noVNC + FastAPI control server + demo form.
│   ├── setup.sh         Idempotent installer; runs once per sandbox.
│   ├── control_server.py FastAPI app exposing screenshot/click/type/scroll/key/...
│   └── form/            Demo expense-report form served on :8080.
└── openai/              Azure OpenAI computer-use-preview demo (ready).
    └── python/
        ├── computer_use.py  Main script: boot, install, run agent, verify.
        ├── aca_computer.py  Adapter: OpenAI computer actions -> control server.
        └── requirements.txt
```

The shared desktop image is vendor-neutral — the same `setup.sh` and
`control_server.py` work for any computer-use agent that needs primitives
like screenshot/click/type. An `anthropic/` sibling (using Claude
computer-use against the same desktop) is planned next.

## How it composes the sandbox guides

- **[guide 01](../../guides/01-sandboxes)** — `begin_create_sandbox` + `exec`
  to boot the desktop and run the installer.
- **[guide 06](../../guides/06-ports)** — `add_port(7000)` for the agent's
  control channel, `add_port(6080)` for the noVNC view.
- **[guide 07](../../guides/07-files)** — `write_file` to upload the
  desktop image into the sandbox.
- **[guide 08](../../guides/08-egress)** — `set_egress_default("Deny")` so
  the agent literally cannot reach the internet. The demo target is the
  form on `localhost:8080` inside the sandbox itself.
- **[guide 03](../../guides/03-disks)** — optional next step: bake
  `setup.sh`'s output into a disk image so subsequent boots skip the
  4-minute `apt install`.

## What the demo does

Two things you can see, side by side:

1. In your **terminal**, the script prints each model turn:
   `turn 1: action=screenshot`, `turn 2: action=click`, …
2. In your **browser** (the noVNC URL the script prints), you watch the
   Chromium window: cursor drifts to fields, typing appears, dropdowns
   open, "Submit expense report" gets clicked, the green "Submitted."
   banner appears.

After the agent declares it's done, the script reads
`/tmp/submission.json` from inside the sandbox and prints the totals it
parsed back out — so you can verify the agent actually filled the form
correctly, not just *clicked things that looked right*.

## How to run

Pick your vendor:

- **[openai/](openai/README.md)** — Azure OpenAI `computer-use-preview` deployment.

## Production tips

- **Bake a disk image.** First boot installs ~600 MB of apt packages
  (Chromium, Xvfb, noVNC, …). Use [guide 03](../../guides/03-disks)
  (`begin_commit`) once and pass `disk_id=` on subsequent runs to drop
  boot to ~10 seconds.
- **Run agents in parallel.** One desktop per task. Sandboxes are
  ephemeral and cheap — `asyncio.gather` over N `begin_create_sandbox`
  calls scales linearly. See
  [scenarios/04-swarms](../04-swarms/README.md) for the pattern.
- **Record sessions.** Add `ffmpeg -f x11grab -i :99 …` to `setup.sh`
  and write the `.mp4` to an AzureBlob volume mount (see
  [guide 04](../../guides/04-volumes)) for a durable audit trail.
- **Always deny egress by default.** The demo uses no allow rules at
  all (the target is a localhost form). For real workloads driving
  external sites, allow only the site(s) you actually need.
- **Computer use is slow and expensive.** Each turn is one model
  round-trip plus a vision-token-heavy screenshot. Mix in DOM/tool-based
  approaches (Playwright as MCP) for steps that don't need vision.
