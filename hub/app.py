"""
Orin Home Hub — backend (v3)
----------------------------
Flask server that:
  1. Streams the USB camera as an MJPEG feed (proper JPEG boundary parsing,
     multi-client broadcast via per-client queues).
  2. Accepts browser-microphone audio, converts to WAV, and plays on Sonos.
  3. Reports system status.

Fixes over v2:
  - MJPEG frames are now delimited by FF D8 / FF D9 JPEG markers so browsers
    receive complete, decodable images instead of raw split chunks.
  - A background reader thread feeds all connected stream clients; multiple
    browser tabs no longer corrupt each other's byte streams.
  - Sonos speaker is cached for 60 s to avoid blocking discover() on every
    HTTP request.
  - Thread-safe camera management via a lock.
  - Sonos None-check before play, volume clamped 0-100, audio path traversal
    protection, read file once for correct Content-Length.
"""

import atexit
import os
import queue
import signal
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from soco import discover
from werkzeug.serving import make_server

# ── Config ──────────────────────────────────────────────────────────
CAM_DEVICE = os.environ.get("ORIN_CAM", "/dev/video0")
RESOLUTION = {"width": 640, "height": 480, "fps": 10}
AUDIO_DIR = Path("/tmp/orin_audio")
PORT = 5001
HTTPS_PORT = 5443
HOST = "0.0.0.0"

# Cert lives next to app.py so it survives restarts.
_HUB_DIR = Path(__file__).parent
CERT_FILE = _HUB_DIR / "cert.pem"
KEY_FILE  = _HUB_DIR / "key.pem"

# Resolve the machine's LAN IP once at startup so Sonos (a physical device on
# the local network) always receives a reachable URL, regardless of how the
# browser connected (localhost, Tailscale hostname, etc.).
def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _s:
            _s.connect(("8.8.8.8", 80))
            return _s.getsockname()[0]
    except OSError:
        return "127.0.0.1"

_HOST_IP = _lan_ip()

AUDIO_DIR.mkdir(exist_ok=True)

# ── Camera — single ffmpeg, broadcast to all streaming clients ───────
_camera_lock = threading.Lock()
_camera_proc = None

_subscribers_lock = threading.Lock()
_frame_subscribers: list[queue.Queue] = []


def _camera_reader_thread(proc: subprocess.Popen) -> None:
    """Parse raw ffmpeg MJPEG output into complete JPEG frames and broadcast."""
    buf = b""
    SOI = b"\xff\xd8"
    EOI = b"\xff\xd9"

    while True:
        try:
            chunk = proc.stdout.read(32768)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk

        # Extract every complete JPEG frame found in the buffer.
        while True:
            start = buf.find(SOI)
            if start == -1:
                buf = b""
                break
            end = buf.find(EOI, start + 2)
            if end == -1:
                buf = buf[start:]   # keep from SOI onwards for next read
                break
            end += 2                # include EOI bytes
            frame = buf[start:end]
            buf = buf[end:]
            with _subscribers_lock:
                for q in list(_frame_subscribers):
                    try:
                        q.put_nowait(frame)
                    except queue.Full:
                        pass        # slow client — drop frame rather than block


def _start_camera() -> None:
    """Start ffmpeg + reader thread if not already running."""
    global _camera_proc
    with _camera_lock:
        if _camera_proc and _camera_proc.poll() is None:
            return
        cmd = [
            "ffmpeg",
            "-f", "v4l2",
            "-framerate", str(RESOLUTION["fps"]),
            "-video_size", f'{RESOLUTION["width"]}x{RESOLUTION["height"]}',
            "-input_format", "mjpeg",
            "-i", CAM_DEVICE,
            "-q:v", "8",
            "-r", str(RESOLUTION["fps"]),
            "-f", "mjpeg",
            "-hide_banner",
            "-loglevel", "error",
            "pipe:1",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
        _camera_proc = proc
        threading.Thread(
            target=_camera_reader_thread, args=(proc,), daemon=True
        ).start()
        print(f"    Camera started  → {CAM_DEVICE}  PID={proc.pid}", flush=True)


# Keep the old name used by atexit and the API routes.
def _ensure_camera() -> None:
    _start_camera()


def _kill_camera():
    """Force-kill ffmpeg process group."""
    global _camera_proc
    with _camera_lock:
        proc = _camera_proc
        if not proc:
            return
        _camera_proc = None
    pid = proc.pid
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    print(f"    Camera killed   PID={pid}", flush=True)


# ── Sonos — cached discovery ─────────────────────────────────────────
_sonos_lock = threading.Lock()
_sonos_speaker = None
_sonos_last_discover: float = 0.0
SONOS_CACHE_TTL = 60.0   # seconds — avoids 5 s blocking discover on every request


def sonos():
    """Return a cached Sonos speaker, re-discovering only when the TTL expires."""
    global _sonos_speaker, _sonos_last_discover
    with _sonos_lock:
        now = time.monotonic()
        if _sonos_speaker is not None and (now - _sonos_last_discover) < SONOS_CACHE_TTL:
            return _sonos_speaker
        speakers = discover(timeout=5)
        _sonos_speaker = list(speakers)[0] if speakers else None
        _sonos_last_discover = now
        return _sonos_speaker


# ── Flask app ───────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", host=request.host.split(":")[0], port=PORT)


@app.route("/stream")
def stream():
    """MJPEG stream — each client gets its own queue fed by the reader thread."""
    _start_camera()
    client_q: queue.Queue = queue.Queue(maxsize=3)
    with _subscribers_lock:
        _frame_subscribers.append(client_q)

    def generate_frames():
        try:
            while True:
                try:
                    frame = client_q.get(timeout=5)
                except queue.Empty:
                    # ffmpeg may have died — try to restart
                    _start_camera()
                    continue
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    + frame + b"\r\n"
                )
        except GeneratorExit:
            pass
        finally:
            with _subscribers_lock:
                try:
                    _frame_subscribers.remove(client_q)
                except ValueError:
                    pass

    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=--frame",
    )


@app.route("/api/status")
def status():
    resp = {"camera": None, "sonos": None}
    # Thread-safe snapshot of the camera process
    with _camera_lock:
        proc = _camera_proc
    if proc and proc.poll() is None:
        resp["camera"] = {
            "device": CAM_DEVICE,
            "fps": RESOLUTION["fps"],
            "resolution": f'{RESOLUTION["width"]}x{RESOLUTION["height"]}',
            "pid": proc.pid,
        }
    try:
        speaker = sonos()
    except Exception:
        speaker = None
    if speaker:
        try:
            info = speaker.get_speaker_info()
            resp["sonos"] = {
                "name": info.get("zoneName", speaker.player_name),
                "ip": speaker.ip_address,
                "status": speaker.get_current_transport_info().get("current_transport_state"),
                "volume": speaker.volume,
            }
        except Exception as e:
            resp["sonos"] = {"error": str(e)}
    else:
        resp["sonos"] = {"error": "No speakers found"}
    return jsonify(resp)


@app.route("/api/sonos/volume", methods=["GET", "POST"])
def sonos_volume():
    sp = sonos()
    if not sp:
        return jsonify({"error": "Sonos not found"}), 504
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        try:
            vol = max(0, min(100, int(data.get("volume", 50))))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid volume value"}), 400
        sp.volume = vol
        return jsonify({"status": "ok", "volume": vol})
    return jsonify({"volume": sp.volume})


@app.route("/api/audio/play", methods=["POST"])
def play_audio_on_sonos():
    """Accept browser audio blob, convert to WAV, play on Sonos."""
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    # Resolve Sonos before touching the filesystem so we fail fast.
    sp = sonos()
    if not sp:
        return jsonify({"error": "Sonos not found"}), 504

    audio_blob = request.files["audio"]
    raw_path = AUDIO_DIR / "input.tmp"
    wav_path = AUDIO_DIR / "latest.wav"
    audio_blob.save(raw_path)

    # Convert to WAV: mono 44100 Hz (Sonos compatible)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(raw_path),
                "-ac", "1",
                "-ar", "44100",
                "-acodec", "pcm_s16le",
                str(wav_path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"Audio conversion failed: {e.stderr.decode()}"}), 500

    try:
        # stop() raises UPnP 701 if Sonos is already stopped/paused.
        # play_uri() handles the state transition itself, so we can skip stop().
        try:
            sp.stop()
        except Exception:
            pass
        audio_url = f"http://{_HOST_IP}:{PORT}/audio/latest.wav"
        sp.play_uri(audio_url)
        return jsonify({"status": "playing", "sonos": sp.player_name, "url": audio_url})
    except Exception as e:
        return jsonify({"error": f"Sonos error: {str(e)}"}), 500


@app.route("/audio/<filename>")
def audio_file(filename):
    # Use only the basename to prevent path traversal attacks.
    safe_name = Path(filename).name
    file_path = AUDIO_DIR / safe_name
    try:
        data = file_path.read_bytes()
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    return Response(
        data,
        mimetype="audio/wav",
        headers={"Cache-Control": "no-cache", "Content-Length": str(len(data))},
    )


@app.route("/api/camera/stop", methods=["POST"])
def stop_camera():
    """Stop the camera and free /dev/video0."""
    _kill_camera()
    return jsonify({"status": "ok"})


@app.route("/api/camera/start", methods=["POST"])
def restart_camera():
    """Manually start the camera (if not already running)."""
    _ensure_camera()
    return jsonify({"status": "ok"})


# ── Shutdown ───────────────────────────────────────────────────────
atexit.register(_kill_camera)


def _start_https_server() -> None:
    """Run a TLS copy of the same Flask app on HTTPS_PORT (daemon thread)."""
    if not (CERT_FILE.exists() and KEY_FILE.exists()):
        print(f"    HTTPS skipped   — cert.pem / key.pem not found in {_HUB_DIR}", flush=True)
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
    print(f"  🏠  Orin Home Hub")
    print(f"  📷  Camera → {CAM_DEVICE}")
    print(f"  🔊  Sonos  → auto-discover (cached {int(SONOS_CACHE_TTL)} s)")
    print(f"  🌐  http://0.0.0.0:{PORT}")
    print(f"  🔒  https://0.0.0.0:{HTTPS_PORT}  (local — self-signed cert)")
    print(f"  🔒  https://{os.environ.get('TAILSCALE_HOSTNAME', 'ubuntu.tail6609df.ts.net')}  (Tailscale)")
    _start_camera()
    threading.Thread(target=_start_https_server, daemon=True).start()
    app.run(host=HOST, port=PORT, threaded=True)
