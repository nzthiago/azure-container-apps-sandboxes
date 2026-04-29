# SSH Setup

## How Sandbox SSH Works

- No port 22, no SSH keys, no SSH daemon
- WebSocket connection to the sandbox management API
- Authenticated via your `az login` token
- Full TTY: colors, tab completion, vim, tmux all work

## Option 1: Node.js (recommended)

### Prerequisites check
```bash
node --version    # need 18+
npm list ws       # need ws package
```

### Install if missing
```bash
# Node.js
# Windows: winget install -e --id OpenJS.NodeJS.LTS
# macOS:   brew install node@22
# Linux:   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt-get install -y nodejs

# ws package
npm install ws
```

### Connect
```bash
node plugin/skills/azure-sandbox/assets/ssh.mjs <sandbox-id> -g <rg> -s <sandbox-group>
```

### If agent is setting up SSH for a user:
1. Check `node --version` — if missing, show install instructions above
2. Check `npm list ws` — if missing, run `npm install ws`
3. Run the ssh.mjs command

## Option 2: az CLI

```bash
az sandbox ssh -g <rg> -s <sandbox-group> --id <sandbox-id>
```

No extra dependencies — uses the Python SDK. May be less responsive on Windows.

## tmux (persistent sessions)

```bash
# Inside the sandbox — install tmux first:
apt-get update -qq && apt-get install -y -qq tmux

# Start a named session:
tmux new -s work           # start
# ... do your work ...
# Ctrl+B, then D           # detach (session keeps running)

# Later, SSH back in:
tmux attach -t work        # reattach
tmux ls                    # list sessions
```

Sessions survive SSH disconnects and sandbox suspend/resume.
