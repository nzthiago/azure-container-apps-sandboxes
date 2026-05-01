# Getting Started with Sandboxes

Step-by-step walkthrough of the full sandbox lifecycle using the ACA CLI.

## Prerequisites

```bash
az login
npm install -g @azure/aca-cli
```

## 1. Create resource group + sandbox group

```bash
az group create --name sandbox-lab-rg --location westus2 -o none
aca sandboxgroup create --name sandbox-lab-sg --location westus2 -g sandbox-lab-rg
```

## 2. Create sandbox

```bash
aca sandbox create --disk ubuntu --wait -g sandbox-lab-rg --group sandbox-lab-sg -o json
```

Save the sandbox ID from the output for the following steps:

```bash
SANDBOX_ID=<id-from-output>
```

## 3. Execute a command

```bash
aca sandbox exec --id $SANDBOX_ID -c "echo Hello && whoami && uname -a" -g sandbox-lab-rg --group sandbox-lab-sg
```

## 4. File operations

Write a file into the sandbox:

```bash
echo "Hello from ACA CLI!" > /tmp/hello.txt
aca sandbox fs write --id $SANDBOX_ID --path /tmp/hello.txt --file /tmp/hello.txt -g sandbox-lab-rg --group sandbox-lab-sg
```

Read it back:

```bash
aca sandbox fs cat --id $SANDBOX_ID --path /tmp/hello.txt -g sandbox-lab-rg --group sandbox-lab-sg
```

## 5. Expose a port

```bash
aca sandbox port add --id $SANDBOX_ID --port 8080 --anonymous -g sandbox-lab-rg --group sandbox-lab-sg -o json
```

## 6. Stats + Snapshot

Get resource stats:

```bash
aca sandbox stats --id $SANDBOX_ID -g sandbox-lab-rg --group sandbox-lab-sg -o json
```

Create a snapshot:

```bash
aca sandbox snapshot create --sandbox-id $SANDBOX_ID --name getting-started -g sandbox-lab-rg --group sandbox-lab-sg -o json
```

## 7. Stop + Resume

Stop the sandbox:

```bash
aca sandbox stop --id $SANDBOX_ID -g sandbox-lab-rg --group sandbox-lab-sg
```

Resume it:

```bash
aca sandbox resume --id $SANDBOX_ID -g sandbox-lab-rg --group sandbox-lab-sg
```

Verify it came back:

```bash
aca sandbox exec --id $SANDBOX_ID -c "echo Back from suspend!" -g sandbox-lab-rg --group sandbox-lab-sg
```

## 8. Clean up

```bash
aca sandbox delete --id $SANDBOX_ID --yes -g sandbox-lab-rg --group sandbox-lab-sg
aca sandboxgroup delete --name sandbox-lab-sg --yes -g sandbox-lab-rg
az group delete --name sandbox-lab-rg --yes --no-wait
```
