# Deploy a Web App to a Sandbox

Deploy a Node.js web server to a sandbox, expose it publicly, and test it.

## Prerequisites

```bash
az login
npm install -g https://github.com/Azure-Samples/azure-container-apps-sandboxes/releases/download/v0.1.0b1/azure-aca-cli-1.0.0-beta.1.tgz
```

## 1. Create resources

```bash
az group create --name sandbox-lab-rg --location westus2 -o none
aca sandboxgroup create --name sandbox-lab-sg --location westus2 -g sandbox-lab-rg
aca sandbox create --disk node-24 --wait -g sandbox-lab-rg --group sandbox-lab-sg -o json
```

Save the sandbox ID:

```bash
SANDBOX_ID=<id-from-output>
```

## 2. Upload app

Create a local file with your app code:

```bash
cat > /tmp/index.js << 'EOF'
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
EOF
```

Upload it to the sandbox:

```bash
aca sandbox fs write --id $SANDBOX_ID --path /app/index.js --file /tmp/index.js -g sandbox-lab-rg --group sandbox-lab-sg
```

## 3. Start the server

```bash
aca sandbox exec --id $SANDBOX_ID -c "cd /app && nohup node index.js > /dev/null 2>&1 &" -g sandbox-lab-rg --group sandbox-lab-sg
```

Test locally inside the sandbox:

```bash
aca sandbox exec --id $SANDBOX_ID -c "sleep 2 && curl -s http://localhost:8080" -g sandbox-lab-rg --group sandbox-lab-sg
```

## 4. Expose port

```bash
aca sandbox port add --id $SANDBOX_ID --port 8080 --anonymous -g sandbox-lab-rg --group sandbox-lab-sg -o json
```

Copy the public URL from the output and test it in your browser or with curl.

## 5. Clean up

```bash
aca sandbox delete --id $SANDBOX_ID --yes -g sandbox-lab-rg --group sandbox-lab-sg
aca sandboxgroup delete --name sandbox-lab-sg --yes -g sandbox-lab-rg
az group delete --name sandbox-lab-rg --yes --no-wait
```
