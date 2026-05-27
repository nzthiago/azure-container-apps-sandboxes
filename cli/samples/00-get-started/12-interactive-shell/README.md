# 12 - Interactive shell

`aca sandbox shell` drops you into a real PTY inside a running sandbox —
prompt, history, job control, the works. Useful for poking at a
misbehaving sandbox, debugging a failed run, or just exploring what a
disk image actually ships with. Faster than scripting `exec` calls when
you're still figuring out what command to run.

This is **CLI-only** — the Python SDK exposes `sandbox.exec(...)` for
one-shot commands but the `aca` CLI is what handles the PTY plumbing for
an interactive terminal session. If you need a shell from Python, shell
out to `aca sandbox shell` (which is exactly what this guide demonstrates,
just driven by hand).

Choose your style:

- [`cli/`](cli/) - `aca` CLI (bash)

Reads configuration from `samples/.env` (see [`../../setup/`](../../setup/)).

## What you'll see

```
==> Creating sandbox...
    sandbox: 91d7...
==> Opening interactive shell. Type 'exit' (or Ctrl-D) to leave.
root@adc-sandbox:/# whoami
root
root@adc-sandbox:/# echo "hi from inside the sandbox"
hi from inside the sandbox
root@adc-sandbox:/# exit
==> Shell closed. Deleting sandbox 91d7...
==> Done.
```

## When to use this vs `exec`

| You want to... | Use |
|---|---|
| Run one command and capture output | `aca sandbox exec -c "..."` (or `sandbox.exec(...)` from Python) |
| Poke around, try multiple commands, debug live | `aca sandbox shell` |
| Run an editor (`vim`, `nano`) interactively | `aca sandbox shell` |
| Pipe a script in non-interactively | `aca sandbox exec` |

## Tips

- Pass `--command /bin/sh` if the image doesn't have bash (the default).
- You can target by label too: `aca sandbox shell -l name=my-sandbox`.
- The shell session runs as root by default — same as `exec`.
- Closing the terminal (Ctrl-D / `exit`) returns control to the script,
  which then cleans up the sandbox. Use Ctrl-C in the parent terminal if
  you need to abort hard; the script's trap/finally still deletes the sandbox.
