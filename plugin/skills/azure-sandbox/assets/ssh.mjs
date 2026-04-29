#!/usr/bin/env node
/**
 * SSH into a sandbox — interactive WebSocket shell.
 *
 * Usage:
 *   node ssh.mjs <sandbox-id> -g <resource-group> -s <sandbox-group>
 *
 * Requires: az login, Node.js 18+, ws package (npm install ws)
 * Press Ctrl+D to exit.
 */

import { execSync } from "child_process";

const DATA_PLANE_HOST = "management.azuredevcompute.io";
const DATA_PLANE_SCOPE = "https://management.azuredevcompute.io/AzureDevCompute.Management.ReadWrite.All";

const args = process.argv.slice(2);
const sandboxId = args[0];
let rg, sg;
for (let i = 1; i < args.length; i++) {
  if (args[i] === "-g" || args[i] === "--resource-group") rg = args[++i];
  if (args[i] === "-s" || args[i] === "--sandbox-group") sg = args[++i];
}

if (!sandboxId || !rg || !sg) {
  console.error("Usage: node ssh.mjs <sandbox-id> -g <resource-group> -s <sandbox-group>");
  process.exit(1);
}

let sub;
try {
  sub = execSync('az account show --query id -o tsv', { encoding: "utf8", stdio: ["pipe", "pipe", "ignore"] }).trim();
} catch {
  console.error("Error: run 'az login' first");
  process.exit(1);
}

let token;
try {
  token = execSync(
    `az account get-access-token --scope "${DATA_PLANE_SCOPE}" --query accessToken -o tsv`,
    { encoding: "utf8", stdio: ["pipe", "pipe", "ignore"] }
  ).trim();
} catch {
  console.error("Error: failed to get access token. Run 'az login' first.");
  process.exit(1);
}

let WebSocket;
try {
  WebSocket = (await import("ws")).default;
} catch {
  console.error("Error: 'ws' package not found. Install: npm install ws");
  process.exit(1);
}

const wsUrl = `wss://${DATA_PLANE_HOST}/subscriptions/${sub}/resourceGroups/${rg}/sandboxGroups/${sg}/sandboxes/${sandboxId}/exec/stream`;

console.log(`Connecting to ${sandboxId.substring(0, 12)}...`);
console.log("Press Ctrl+D to exit\n");

const ws = new WebSocket(wsUrl, {
  headers: { Authorization: `Bearer ${token}` },
});

const cols = process.stdout.columns || 80;
const rows = process.stdout.rows || 24;

ws.on("open", () => {
  ws.send(JSON.stringify({
    type: "start",
    start: {
      command: "/bin/bash",
      environment: { TERM: "xterm-256color", LANG: "C.UTF-8", LC_ALL: "C.UTF-8" },
      tty: true, stdin: true, height: rows, width: cols,
    },
  }));

  if (process.stdin.isTTY) process.stdin.setRawMode(true);
  process.stdin.resume();
  process.stdin.on("data", (data) => {
    if (data.length === 1 && data[0] === 0x04) { ws.close(); return; }
    ws.send(JSON.stringify({ type: "stdin", data: data.toString("base64") }));
  });
});

ws.on("message", (raw) => {
  try {
    const msg = JSON.parse(raw.toString());
    if (msg.type === "stdout" || msg.type === "stderr") {
      const output = Buffer.from(msg.data || "", "base64");
      (msg.type === "stdout" ? process.stdout : process.stderr).write(output);
    } else if (msg.type === "exit_code") { ws.close(); }
    else if (msg.type === "error") { console.error(`\nError: ${JSON.stringify(msg)}`); ws.close(); }
  } catch {}
});

ws.on("close", () => {
  if (process.stdin.isTTY) process.stdin.setRawMode(false);
  console.log("\nDisconnected");
  process.exit(0);
});

ws.on("error", (err) => {
  console.error(`Connection error: ${err.message}`);
  process.exit(1);
});
