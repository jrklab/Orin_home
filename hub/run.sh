#!/usr/bin/env bash
# Orin Home Hub — launcher (consolidated)
#
# Works in two modes, detected automatically:
#   systemd  ($INVOCATION_ID is set) — execs Flask in the foreground so systemd
#            tracks the process directly; systemd handles restarts.
#   manual   (interactive / tmux)    — backgrounds Flask via nohup, writes a
#            PID file, kills any old instance on re-run.
#
# Usage:
#   ./run.sh              # manual run
#   systemctl restart orin-hub   # via systemd unit
set -euo pipefail

HUB_PORT=5001
HTTPS_PORT=5443
HUB_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$HUB_DIR/.hub.pid"

echo "🏠  Orin Home Hub"

# ── 0. Kill old instance ─────────────────────────────────────────────
# Try PID file first, then pkill as fallback.
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "   Stopping old instance (PID ${OLD_PID})…"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PIDFILE"
fi
# Belt-and-suspenders: catch any stray process holding the port.
pkill -f "python3.*app\.py" 2>/dev/null || true
sleep 1

# ── 1. Self-signed cert (generated once, valid 10 years) ────────────
if [ ! -f "$HUB_DIR/cert.pem" ] || [ ! -f "$HUB_DIR/key.pem" ]; then
    echo "   Generating self-signed TLS certificate…"
    LOCAL_IP=$(hostname -I | awk '{print $1}')
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$HUB_DIR/key.pem" \
        -out    "$HUB_DIR/cert.pem" \
        -days 3650 -nodes \
        -subj "/CN=orin-hub" \
        -addext "subjectAltName=IP:${LOCAL_IP},IP:127.0.0.1,DNS:localhost" \
        2>/dev/null
    echo "   ✅ Certificate created (${LOCAL_IP})"
fi

# ── 2. Tailscale serve ───────────────────────────────────────────────
TAILNAME=$(tailscale status --json 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))" \
    2>/dev/null || echo "ubuntu.tail")
tailscale serve reset 2>/dev/null || true
tailscale serve --bg "http://127.0.0.1:${HUB_PORT}" >> "$HUB_DIR/serve.log" 2>&1

# ── 3. Print access URLs ─────────────────────────────────────────────
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "   HTTP      : http://localhost:${HUB_PORT}"
echo "   HTTPS     : https://${LOCAL_IP}:${HTTPS_PORT}  ← laptop (accept cert once)"
echo "   Tailscale : https://${TAILNAME}   ← no cert warning, mic works"
echo ""

# ── 4. Start Flask ───────────────────────────────────────────────────
cd "$HUB_DIR"
if [ -n "${INVOCATION_ID:-}" ]; then
    # Running under systemd — exec Flask so systemd tracks its PID directly.
    echo "   [systemd] Starting Flask in foreground…"
    exec python3 "$HUB_DIR/app.py"
else
    # Manual run — background via nohup, write PID file for future kills.
    nohup python3 "$HUB_DIR/app.py" >> "$HUB_DIR/hub.log" 2>&1 &
    FLASK_PID=$!
    echo "$FLASK_PID" > "$PIDFILE"
    echo "   Waiting for Flask to bind…"
    for i in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:${HUB_PORT}/api/status" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    echo "   ✅ Flask running in background (PID ${FLASK_PID})"
    echo "   Logs: $HUB_DIR/hub.log"
fi
