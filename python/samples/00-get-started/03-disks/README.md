# 03 - Disks (Python)

```bash
pip install -r requirements.txt
python disks.py
```

> Takes ~10-20 min total — two disk builds back-to-back.

## What this shows

| API | What it does |
|---|---|
| `client.list_public_disk_images()` | Discovery — names you can pass as `disk="..."` on create |
| `client.begin_create_disk_image("alpine:3.19", name=...)` | Build a custom disk from a container image |
| `client.list_disk_images()` / `get_disk_image(id)` | Inventory + lookup of your private disks |
| `client.begin_create_sandbox(disk_id=...)` | Boot a sandbox from a custom disk |
| `sandbox.begin_commit(name=...)` | Freeze a running sandbox into a new disk image |
| `client.delete_disk_image(id)` | Remove a disk image |
