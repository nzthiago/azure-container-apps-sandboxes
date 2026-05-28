"""Egress — default-deny + host allowlist.

Demonstrates the canonical egress lockdown pattern:
  1. Set default action to Deny (block everything)
  2. Add Allow rules for trusted hosts
  3. Verify with curl from inside the sandbox

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region


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
        print("==> Booting sandbox...")
        sandbox = client.begin_create_sandbox(labels={"guide": "egress"}).result()
        print(f"    sandbox: {sandbox.sandbox_id}")

        print("==> Baseline: curl an arbitrary host (should succeed, default is Allow)...")
        r = sandbox.exec("curl -sS -o /dev/null -w '%{http_code}' --max-time 8 https://example.com")
        print(f"    HTTP {r.stdout.strip()}  exit={r.exit_code}")

        print("==> Locking down: set default action = Deny...")
        sandbox.set_egress_default("Deny")

        print("==> Allowlist *.github.com...")
        policy = sandbox.add_egress_host_rule("*.github.com", action="Allow")
        print(f"    default={policy.default_action}  host_rules={len(policy.host_rules)}")

        # Egress policy changes propagate to the dataplane asynchronously.
        # Give it a few seconds before we test enforcement.
        import time
        time.sleep(10)

        print("==> Verifying: denied host now blocked...")
        r = sandbox.exec("curl -sS -o /dev/null -w '%{http_code}' --max-time 8 https://example.com || echo CURL_FAIL")
        # When egress denies the host, you'll see a non-200 from the proxy
        # (403 / 502 / etc) or curl will fail outright.
        code = r.stdout.strip()
        blocked = code != "200"
        print(f"    example.com -> HTTP {code}  blocked={blocked}")

        print("==> Verifying: github.com allowed...")
        r = sandbox.exec("curl -sS -o /dev/null -w '%{http_code}' --max-time 8 https://api.github.com")
        print(f"    api.github.com -> HTTP {r.stdout.strip()}  exit={r.exit_code}")

        print("==> Audit: get_egress_decisions()...")
        decisions = sandbox.get_egress_decisions()
        entries = getattr(decisions, "entries", None) or []
        print(f"    {len(entries)} decision entr{'y' if len(entries)==1 else 'ies'} logged")
        for entry in entries[:5]:
            print(f"      {entry}")
    finally:
        if sandbox is not None:
            print(f"==> Deleting sandbox {sandbox.sandbox_id}...")
            sandbox.delete()
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
