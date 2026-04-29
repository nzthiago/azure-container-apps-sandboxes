"""Deploy a Web App to a Sandbox.

Usage:
    python deploy-web-app.py -g <resource-group> -s <sandbox-group> -l <location>
    python deploy-web-app.py  # uses defaults
"""

import argparse
import json
import subprocess
import time

parser = argparse.ArgumentParser(description="Deploy Web App to Sandbox")
parser.add_argument("-g", "--resource-group", default=None, help="Resource group")
parser.add_argument("-s", "--sandbox-group", default=None, help="Sandbox group name")
parser.add_argument("-l", "--location", default="westus2", help="Azure region")
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

from azure.sandbox import SandboxClient
from azure.mgmt.sandbox import SandboxGroupManagementClient
client = SandboxClient(subscription_id=subscription_id, resource_group=rg)
mgmt = SandboxGroupManagementClient(subscription_id=subscription_id, resource_group=rg)

# 1. Create resources
print("\n1. Creating resources...")
subprocess.run(["az", "group", "create", "--name", rg, "--location", location, "-o", "none"], check=True)
group = mgmt.create_group(sg, location=location)
print(f"   Group: {group['name']}")

sbx = client.create_sandbox(sg, disk="node-24")
sandbox_id = sbx["id"]
print(f"   Sandbox: {sandbox_id}")

# 2. Upload app
print("\n2. Uploading app...")
app_code = """
const http = require('http');
const os = require('os');

http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify({
    message: 'Hello from sandbox!',
    hostname: os.hostname(),
    uptime: process.uptime(),
    path: req.url,
  }, null, 2));
}).listen(8080, '0.0.0.0', () => console.log('Server on :8080'));
"""
client.write_file(sandbox_id, sg, "/app/index.js", app_code.strip())
print("   Uploaded /app/index.js")

# 3. Start server
print("\n3. Starting server...")
client.exec(sandbox_id, sg, "cd /app && nohup node index.js > /dev/null 2>&1 &")
time.sleep(2)
result = client.exec(sandbox_id, sg, "curl -s http://localhost:8080")
print(f"   Local: {result['stdout'].strip()[:80]}...")

# 4. Expose port
print("\n4. Exposing port 8080...")
ports = client.add_port(sandbox_id, sg, 8080, anonymous=True)
url = None
for p in ports.get("ports", []):
    url = p.get("url")
    print(f"   URL: {url}")

# 5. Test
if url:
    print("\n5. Testing public URL...")
    time.sleep(3)
    import urllib.request
    response = urllib.request.urlopen(url)
    print(f"   Response: {response.read().decode()[:100]}")

# 6. Clean up
print("\n6. Cleaning up...")
client.delete_sandbox(sandbox_id, sg)
print("   Deleted sandbox")
mgmt.delete_group(sg)
print("   Deleted group")
subprocess.run(["az", "group", "delete", "--name", rg, "--yes", "--no-wait"], check=True)
print("   Deleting resource group (async)")

print("\nDone!")
