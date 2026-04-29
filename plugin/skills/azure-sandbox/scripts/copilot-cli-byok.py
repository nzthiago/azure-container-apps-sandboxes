"""Run GitHub Copilot CLI in a Sandbox with Azure OpenAI BYOK.

Creates an ubuntu sandbox, installs Copilot CLI, and configures it to use
Azure OpenAI (or any OpenAI-compatible endpoint) via BYOK. No GitHub auth
needed — set COPILOT_OFFLINE=true for full air-gap.

For zero-trust: pass the API key via egress transform rules instead of
directly, so the key never enters the sandbox.

Usage:
    python copilot-cli-byok.py --aoai-endpoint <url> --aoai-key <key> --model <name>
    python copilot-cli-byok.py --aoai-endpoint <url> --aoai-key <key> --model <name> --prompt "go yolo"
    python copilot-cli-byok.py --aoai-endpoint <url> --aoai-key <key> --model <name> --zero-trust
"""

import argparse
import json
import shlex
import subprocess

parser= argparse.ArgumentParser(description="Copilot CLI in Sandbox (BYOK)")
parser.add_argument("--aoai-endpoint", required=True,
    help="Azure OpenAI endpoint (e.g. https://<resource>.openai.azure.com/openai/deployments/<deployment>)")
parser.add_argument("--aoai-key", required=True, help="Azure OpenAI API key")
parser.add_argument("--model", required=True, help="Model/deployment name (e.g. gpt-4o)")
parser.add_argument("-g", "--resource-group", default=None, help="Resource group")
parser.add_argument("-s", "--sandbox-group", default=None, help="Sandbox group name")
parser.add_argument("-l", "--location", default="westus2", help="Azure region")
parser.add_argument("--prompt", default=None, help="One-shot prompt (skip interactive)")
parser.add_argument("--zero-trust", action="store_true",
    help="Inject API key via egress rules (key never enters sandbox)")
parser.add_argument("--cleanup", action="store_true",
    help="Delete sandbox group + RG after (default: sandbox only)")
args = parser.parse_args()

account = json.loads(subprocess.run(
    ["az", "account", "show", "-o", "json"],
    capture_output=True, text=True, check=True).stdout)

subscription_id = account["id"]
rg = args.resource_group or "sandbox-lab-rg"
sg = args.sandbox_group or "sandbox-lab-sg"
location = args.location

print(f"User:           {account['user']['name']}")
print(f"Resource Group: {rg}")
print(f"Sandbox Group:  {sg}")
print(f"Model:          {args.model}")
print(f"Zero-trust:     {args.zero_trust}")

from azure.sandbox import SandboxClient
from azure.mgmt.sandbox import SandboxGroupManagementClient
client = SandboxClient(subscription_id=subscription_id, resource_group=rg)
mgmt = SandboxGroupManagementClient(subscription_id=subscription_id, resource_group=rg)

# 1. Create resources (idempotent)
print("\n1. Creating resources...")
subprocess.run(["az", "group", "create", "--name", rg, "--location", location, "-o", "none"], check=True)
try:
    mgmt.create_group(sg, location=location)
    print(f"   Group: {sg} (created)")
except Exception as exc:
    if "already exists" in str(exc).lower() or "conflict" in str(exc).lower():
        print(f"   Group: {sg} (already exists)")
    else:
        raise

sbx = client.create_sandbox(sg, disk="ubuntu", cpu="2000m", memory="4096Mi")
sandbox_id = sbx["id"]
print(f"   Sandbox: {sandbox_id}")

# 2. Install Copilot CLI
print("\n2. Installing Copilot CLI...")
result = client.exec(sandbox_id, sg, "curl -fsSL https://gh.io/copilot-install | bash 2>&1 | tail -2")
print(f"   {result['stdout'].strip()}")

# 3. Configure BYOK
if args.zero_trust:
    # Zero-trust: API key injected via egress, placeholder inside sandbox
    print("\n3. Configuring BYOK (zero-trust — key via egress)...")
    from urllib.parse import urlparse
    host = urlparse(args.aoai_endpoint).hostname
    client.add_egress_transform_rule(
        sandbox_id, sg, host=host,
        headers=[{"operation": "Set", "name": "api-key", "value": args.aoai_key}],
        name="aoai-key-swap",
    )
    print(f"   Egress rule set for {host}")
    api_key_env = "PLACEHOLDER_EGRESS_WILL_SWAP"
else:
    print("\n3. Configuring BYOK...")
    api_key_env = args.aoai_key

copilot_env = " && ".join([
    f"export COPILOT_PROVIDER_BASE_URL={shlex.quote(args.aoai_endpoint)}",
    "export COPILOT_PROVIDER_TYPE=azure",
    f"export COPILOT_PROVIDER_API_KEY={shlex.quote(api_key_env)}",
    f"export COPILOT_MODEL={shlex.quote(args.model)}",
    "export COPILOT_OFFLINE=true",
])

# 4. Run Copilot CLI
if args.prompt:
    print(f"\n4. Running Copilot CLI: {args.prompt}")
    result = client.exec(sandbox_id, sg, f'{copilot_env} && copilot -p {shlex.quote(args.prompt)} 2>&1')
    print(f"   {result['stdout'].strip()}")
else:
    print("\n4. Sandbox ready for Copilot CLI!")
    print(f"   SSH in:  node plugin/skills/azure-sandbox/assets/ssh.mjs {sandbox_id} -g {rg} -s {sg}")
    print(f"            az sandbox ssh -g {rg} -s {sg} --id {sandbox_id}")
    print(f"   Then set env vars and run copilot:")
    print(f"     export COPILOT_PROVIDER_BASE_URL={args.aoai_endpoint}")
    print(f"     export COPILOT_PROVIDER_TYPE=azure")
    print(f"     export COPILOT_PROVIDER_API_KEY=<key>")
    print(f"     export COPILOT_MODEL={args.model}")
    print(f"     export COPILOT_OFFLINE=true")
    print(f"     copilot")
    input("\n   Press Enter to clean up...")

# 5. Clean up
print("\n5. Cleaning up...")
client.delete_sandbox(sandbox_id, sg)
print("   Deleted sandbox")
if args.cleanup:
    mgmt.delete_group(sg)
    print("   Deleted group")
    subprocess.run(["az", "group", "delete", "--name", rg, "--yes", "--no-wait"], check=True)
    print("   Deleting resource group (async)")

print("\nDone!")
