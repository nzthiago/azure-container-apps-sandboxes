# 06 - Ports (Python)

```bash
pip install -r requirements.txt
python ports.py
```

## What this shows

| API | What it does |
|---|---|
| `sandbox.add_port(8080, anonymous=True)` | Open a public URL onto port 8080 |
| `port.url` | The HTTPS URL anyone can hit |
| `sandbox.remove_port(8080)` | Take the URL back down |
