# Interactive Shell

## How Sandbox Shell Works

- No port 22, no SSH keys, no SSH daemon
- WebSocket connection to the sandbox management API
- Authenticated via your `az login` token
- Full TTY: colors, tab completion, vim, tmux all work

## Connect

```bash
aca sandbox shell --id <sandbox-id> -g <rg> --group <sandbox-group>
```

The ACA CLI requires `az login` for authentication. Run `az login` first if you haven't already.

## tmux (persistent sessions)

```bash
# Inside the sandbox — install tmux first:
apt-get update -qq && apt-get install -y -qq tmux

# Start a named session:
tmux new -s work           # start
# ... do your work ...
# Ctrl+B, then D           # detach (session keeps running)

# Later, connect back:
tmux attach -t work        # reattach
tmux ls                    # list sessions
```

Sessions survive disconnects and sandbox suspend/resume.
