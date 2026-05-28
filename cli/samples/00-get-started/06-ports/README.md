# 06 - Ports

Expose a port on a sandbox and reach it from anywhere on the internet.
Anything serving HTTP inside the sandbox — a dev server, an LLM
endpoint, a notebook, a generated web app — becomes addressable from
your laptop, your CI, or an end user. `add_port(anonymous=True)`
returns a public URL; `remove_port` revokes it.

- [`python/`](python/) - Python SDK
- [`cli/`](cli/) - `aca` CLI (bash)

## What it does

1. Start a sandbox
2. Launch a 1-line HTTP server inside the sandbox on `:8080`
3. Call `add_port(8080, anonymous=True)` to get a public URL
4. Curl that URL from your local machine
5. `remove_port(8080)` and tear down
