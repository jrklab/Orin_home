"""
Orin Home Hub — entry point (Task 2 refactor)
---------------------------------------------
This file wires together Flask, SocketIO, blueprints, camera, audio,
and the HTTPS server.  Feature logic lives in the modules imported below.

File layout:
  config.py          — all constants
  extensions.py      — shared SocketIO instance
  camera.py          — USB camera management
  audio.py           — USB speakerphone management
  sonos_utils.py     — Sonos discovery cache
  routes/
    conference.py    — page 1: video + two-way audio
    sonos.py         — page 2: Sonos push-to-talk
    robot.py         — page 3: LeKiwi robot (Task 3)
    misc.py          — page 4: TBD (Task 4)
"""

import atexit
import os
import ssl
import threading

from flask import Flask

import audio
import camera
from config import CERT_FILE, HOST, HTTPS_PORT, KEY_FILE, PORT, USB_AUDIO_DEVICE, AUDIO_IN_RATE
from extensions import socketio

# ── Flask app ────────────────────────────────────────────────────────
app = Flask(__name__)
socketio.init_app(app)

# ── Register blueprints ───────────────────────────────────────────────
from routes.conference import bp as conference_bp
from routes.sonos       import bp as sonos_bp
from routes.robot       import bp as robot_bp
from routes.misc        import bp as misc_bp

app.register_blueprint(conference_bp)
app.register_blueprint(sonos_bp)
app.register_blueprint(robot_bp)
app.register_blueprint(misc_bp)

# ── Cleanup on exit ───────────────────────────────────────────────────
atexit.register(camera.stop)
atexit.register(audio.stop)


# ── HTTPS server ──────────────────────────────────────────────────────
def _start_https_server() -> None:
    """Run an HTTPS + WSS server on HTTPS_PORT using Werkzeug + SSL."""
    from werkzeug.serving import make_server
    if not (CERT_FILE.exists() and KEY_FILE.exists()):
        print(f"    HTTPS skipped   — cert.pem / key.pem not found", flush=True)
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    except ssl.SSLError as e:
        print(f"    HTTPS skipped   — cert load error: {e}", flush=True)
        return
    server = make_server(HOST, HTTPS_PORT, app, ssl_context=ctx, threaded=True)
    print(f"    HTTPS started   → https://0.0.0.0:{HTTPS_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    tailscale_host = os.environ.get("TAILSCALE_HOSTNAME", "ubuntu.tail6609df.ts.net")
    print(f"  🏠  Orin Home Hub")
    print(f"  🎙️  Audio  → {USB_AUDIO_DEVICE}  {AUDIO_IN_RATE} Hz in / {48000} Hz out")
    print(f"  🔊  Sonos  → enabled (page 2)")
    print(f"  🌐  http://0.0.0.0:{PORT}  (video only — mic blocked by browsers on HTTP)")
    print(f"  🔒  https://0.0.0.0:{HTTPS_PORT}  (full audio+video — self-signed cert)")
    print(f"  🔒  https://{tailscale_host}  (Tailscale)")

    camera.start()
    audio.start()
    threading.Thread(target=_start_https_server, daemon=True).start()
    socketio.run(app, host=HOST, port=PORT, allow_unsafe_werkzeug=True)

