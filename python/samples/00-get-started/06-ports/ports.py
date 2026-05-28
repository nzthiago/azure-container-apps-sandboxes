"""Ports - expose port 8080 and hit it from outside the sandbox.

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import (
    SandboxGroupClient,
    endpoint_for_region,
)


def _load_env() -> None:
    """Load samples/.env; exit with a friendly error if it isn't there yet."""
    import sys
    for parent in Path(__file__).resolve().parents:
        env = parent / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            break
    if not os.environ.get("ACA_SANDBOXGROUP_REGION"):
        sys.exit(
            "error: samples/.env is missing required keys. Run:\n"
            "       python samples/sandboxes/setup/python/setup.py"
        )


def main() -> None:
    _load_env()
    credential = DefaultAzureCredential()
    client = SandboxGroupClient(
        endpoint_for_region(os.environ["ACA_SANDBOXGROUP_REGION"]),
        credential,
        subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
        resource_group=os.environ["ACA_RESOURCE_GROUP"],
        sandbox_group=os.environ["ACA_SANDBOX_GROUP"],
    )

    sandbox = None
    try:
        print("==> Creating sandbox...")
        sandbox = client.begin_create_sandbox(disk="ubuntu").result()
        print(f"    sandbox: {sandbox.sandbox_id}")

        print("==> Starting a tiny HTTP server inside the sandbox on :8080...")
        # python3 is in the ubuntu disk; run it detached.
        sandbox.exec(
            "nohup python3 -c \""
            "import http.server, socketserver;"
            "h=http.server.BaseHTTPRequestHandler;"
            "h.do_GET=lambda s: (s.send_response(200), s.end_headers(),"
            " s.wfile.write(b'hello from sandbox\\n'));"
            "socketserver.TCPServer(('0.0.0.0',8080), h).serve_forever()"
            "\" > /tmp/srv.log 2>&1 &"
        )
        time.sleep(2)

        print("==> add_port(8080, anonymous=True)")
        port = sandbox.add_port(8080, anonymous=True)
        url = getattr(port, "url", None)
        print(f"    public URL: {url}")
        if not url:
            raise RuntimeError("add_port did not return a URL")

        print("==> Curling public URL from this machine...")
        # Allow the proxy a few seconds to wire up.
        time.sleep(6)
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode()
        print(f"    response: {body.strip()}")

        print("==> remove_port(8080)")
        sandbox.remove_port(8080)

        print("==> Done.")
    finally:
        if sandbox is not None:
            print(f"==> Deleting sandbox {sandbox.sandbox_id}...")
            sandbox.delete()
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
