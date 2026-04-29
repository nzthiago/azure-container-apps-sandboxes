"""Getting Started with Sandboxes.

Usage:
    python 01-getting-started.py -g <resource-group> -s <sandbox-group> -l <location>
    python 01-getting-started.py  # uses defaults
"""

import argparse
import json
import subprocess
import time

parser = argparse.ArgumentParser(description="Sandbox Getting Started")
parser.add_argument("-g", "--resource-group", default=None, help="Resource group (created if needed)")
parser.add_argument("-s", "--sandbox-group", default=None, help="Sandbox group name (created if needed)")
parser.add_argument("-l", "--location", default="westus2", help="Azure region")
args = parser.parse_args()

account = json.loads(subprocess.run(
    ["az", "account", "show", "-o", "json"],
    capture_output=True, text=True, shell=True).stdout)

subscription_id = account["id"]
rg = args.resource_group or "sandbox-lab-rg"
sg = args.sandbox_group or "sandbox-lab-sg"
location = args.location

print(f"User:           {account['user']['name']}")
print(f"Subscription:   {account['name']} ({subscription_id})")
print(f"Resource Group: {rg}")
print(f"Sandbox Group:  {sg}")
print(f"Location:       {location}")

from azure.sandbox import SandboxClient
from azure.mgmt.sandbox import SandboxGroupManagementClient
client = SandboxClient(subscription_id=subscription_id, resource_group=rg)
mgmt = SandboxGroupManagementClient(subscription_id=subscription_id, resource_group=rg)

# 1. Create resources
print("\n1. Creating resource group + sandbox group...")
subprocess.run(["az", "group", "create", "--name", rg, "--location", location, "-o", "none"], shell=True)
group = mgmt.create_group(sg, location=location)
print(f"   Group: {group['name']}")

# 2. Create sandbox
print("\n2. Creating sandbox...")
sbx = client.create_sandbox(sg, disk="ubuntu")
sandbox_id = sbx["id"]
print(f"   Sandbox: {sandbox_id} state={sbx['state']}")

# 3. Exec
print("\n3. Executing command...")
result = client.exec(sandbox_id, sg, "echo Hello && whoami && uname -a")
print(f"   {result['stdout'].strip()}")

# 4. Files
print("\n4. File operations...")
client.write_file(sandbox_id, sg, "/tmp/hello.txt", "Hello from SDK!")
content = client.read_file(sandbox_id, sg, "/tmp/hello.txt")
print(f"   Written + read: {content.decode()}")

# 5. Port
print("\n5. Adding port 8080...")
ports = client.add_port(sandbox_id, sg, 8080, anonymous=True)
for p in ports.get("ports", []):
    print(f"   port={p['port']}  url={p.get('url', 'n/a')}")

# 6. Stats + Snapshot
print("\n6. Stats + snapshot...")
stats = client.get_stats(sandbox_id, sg)
mem = stats.get("memory", {})
print(f"   Memory: {mem.get('usedBytes', 0) // 1024 // 1024}MB / {mem.get('totalBytes', 0) // 1024 // 1024}MB")
snap = client.create_snapshot(sandbox_id, sg, name="getting-started")
print(f"   Snapshot: {snap.get('id', 'n/a')}")

# 7. Stop + Resume
print("\n7. Stop + resume...")
client.stop_sandbox(sandbox_id, sg)
print("   Stopped")
time.sleep(3)
client.resume_sandbox(sandbox_id, sg)
time.sleep(5)
result = client.exec(sandbox_id, sg, "echo Back from suspend!")
print(f"   {result['stdout'].strip()}")

# 8. Clean up
print("\n8. Cleaning up...")
client.delete_sandbox(sandbox_id, sg)
print("   Deleted sandbox")
mgmt.delete_group(sg)
print("   Deleted group")
subprocess.run(["az", "group", "delete", "--name", rg, "--yes", "--no-wait"], shell=True)
print("   Deleting resource group (async)")

print("\nDone!")
