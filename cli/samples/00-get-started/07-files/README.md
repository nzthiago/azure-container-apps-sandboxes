# 07 - Files

Move data in and out of a sandbox without packaging it into the disk
image. Upload source code or input data, read back generated artifacts,
walk directories — the same shape as the local filesystem, available
to your code from outside the sandbox. The flow most agents reach for
between "create sandbox" and "exec command".

- [`python/`](python/) - Python SDK
- [`cli/`](cli/) - `aca` CLI (bash)

Covers `write_file`, `read_file`, `stat_file`, `list_files`, `mkdir`,
and `delete_file`.
