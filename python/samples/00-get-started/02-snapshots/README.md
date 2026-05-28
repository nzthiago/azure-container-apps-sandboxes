# 02 - Snapshots (Python)

```bash
pip install -r requirements.txt
python snapshots.py
```

## What this shows

| API | What it does |
|---|---|
| `sandbox.create_snapshot(name=...)` | Capture sandbox state |
| `client.begin_create_sandbox(snapshot_id=...)` | Boot a new sandbox from a snapshot |
| `client.delete_snapshot(snap_id)` | Remove the snapshot |
