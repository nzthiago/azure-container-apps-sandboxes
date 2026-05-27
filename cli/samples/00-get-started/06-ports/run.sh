#!/usr/bin/env bash
# Ports - expose port 8080 and hit it from outside the sandbox (aca CLI).

set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do
    dir="$(dirname "$dir")"
done
if [[ -f "$dir/.env" ]]; then
    set -a; . "$dir/.env"; set +a
else
    echo "error: could not find samples/.env - run setup/cli/setup.sh first?" >&2
    exit 1
fi

echo "==> Creating sandbox..."
CREATE_OUTPUT="$(aca sandbox create --disk ubuntu)"
SANDBOX_ID="$(echo "$CREATE_OUTPUT" | sed -n 's/^Created sandbox: //p' | tail -n1)"
[[ -n "$SANDBOX_ID" ]] || { echo "error: could not parse sandbox id" >&2; exit 1; }
echo "    sandbox: $SANDBOX_ID"

cleanup() {
    echo "==> Deleting sandbox $SANDBOX_ID..."
    aca sandbox delete --id "$SANDBOX_ID" --yes >/dev/null || true
}
trap cleanup EXIT

echo "==> Starting tiny HTTP server inside the sandbox on :8080..."
aca sandbox exec --id "$SANDBOX_ID" -c \
  "nohup python3 -c \"import http.server,socketserver; h=http.server.BaseHTTPRequestHandler; h.do_GET=lambda s:(s.send_response(200),s.end_headers(),s.wfile.write(b'hello from sandbox\\n')); socketserver.TCPServer(('0.0.0.0',8080), h).serve_forever()\" > /tmp/srv.log 2>&1 &"
sleep 2

echo "==> aca sandbox port add 8080 --anonymous"
PORT_OUTPUT="$(aca sandbox port add --id "$SANDBOX_ID" --port 8080 --anonymous -o json)"
URL="$(echo "$PORT_OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))")"
echo "    public URL: $URL"
[[ -n "$URL" ]] || { echo "error: no URL in add port response" >&2; exit 1; }

echo "==> Curling public URL from this machine..."
sleep 6
curl -s --max-time 15 "$URL"

echo "==> aca sandbox port remove --port 8080"
aca sandbox port remove --id "$SANDBOX_ID" --port 8080

echo "==> Done."
