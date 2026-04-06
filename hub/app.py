"""
Orin Home Hub — backend (v2)
----------------------------
Flask server that:
  1. Streams the USB camera as an MJPEG feed.
  2. Accepts browser-microphone audio, converts to WAV, and plays on Sonos.
  3. Reports system status.

All camera cleanup now uses a single shared ffmpeg instance with SIGKILL to
prevent zombie processes from holding /dev/video0.
"""

import atexit
import json
import os
import signal
import subprocess
import threading
import sys

from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from soco import discover

# ── Config ──────────────────────────────────────────────────────────
CAM_DEVICE = os.environ.get("ORIN_CAM", "/dev/video0")
RESOLUTION = {"width": 640, "height": 480, "fps": 10}
AUDIO_DIR = Path("/tmp/orin_audio")
PORT = 5001
HOST = "0.0.0.0"

AUDIO_DIR.mkdir(exist_ok=True)

# ── Camera — single shared ffmpeg with reliable kill ───────────────
_camera_proc = None
_camera_thread = None

def _ensure_camera():
    """Start ffmpeg if not already running. Returns the stdout pipe."""
    global _camera_proc
    if _camera_proc and _camera_proc.poll() is None:
        return _camera_proc.stdout
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
    _camera_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"    Camera started  → {CAM_DEVICE}  PID={_camera_proc.pid}", flush=True)
    return _camera_proc.stdout


def _kill_camera():
    """Force-kill ffmpeg process group. Always use SIGKILL — no waiting."""
    global _camera_proc
    proc = _camera_proc
    if not proc:
        return
    _camera_proc = None  # clear immediately so next request starts fresh
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


def start_camera():
    """Alias for backwards compatibility."""
    _kill_camera()

# ── Flask app ───────────────────────────────────────────────────────
app = Flask(__name__)


def sonos():
    speakers = discover(timeout=5)
    if not speakers:
        return None
    return list(speakers)[0]


@app.route("/")
def index():
    return render_template("index.html", host=request.host.split(":")[0], port=PORT)


@app.route("/stream")
def stream():
    """MJPEG camera stream — shared single ffmpeg instance."""

    def generate_frames():
        stdout_pipe = _ensure_camera()
        try:
            while True:
                frame = stdout_pipe.read(65536)
                if not frame:
                    break
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                )
        except GeneratorExit:
            pass
        except Exception:
            pass

    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=--frame",
    )


@app.route("/api/status")
def status():
    resp = {
        "camera": None,
        "sonos": None,
    }
    # Check camera
    if _camera_proc and _camera_proc.poll() is None:
        resp["camera"] = {
            "device": CAM_DEVICE,
            "fps": RESOLUTION["fps"],
            "resolution": f'{RESOLUTION["width"]}x{RESOLUTION["height"]}',
            "pid": _camera_proc.pid,
        }
    # Check Sonos
    import socket
    speaker = None
    try:
        speaker = sonos()
    except Exception:
        pass
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
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        vol = data.get("volume", 50)
        sp = sonos()
        if sp:
            sp.volume = int(vol)
            return jsonify({"status": "ok", "volume": int(vol)})
        return jsonify({"error": "Sonos not found"}), 504
    else:
        sp = sonos()
        if sp:
            return jsonify({"volume": sp.volume})
        return jsonify({"error": "Sonos not found"}), 504


@app.route("/api/audio/play", methods=["POST"])
def play_audio_on_sonos():
    """Accept browser audio blob, convert to WAV, play on Sonos."""
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]
    raw_path = AUDIO_DIR / "input.tmp"
    wav_path = AUDIO_DIR / "latest.wav"
    audio_file.save(raw_path)

    # Convert to WAV: mono 44100 Hz (Sonos compatible)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_path),
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
        sp = sonos()
        sp.stop()
        host_addr = request.host.split(":")[0]
        audio_url = f"http://{host_addr}:{PORT}/audio/latest.wav"
        sp.play_uri(audio_url)
        return jsonify({"status": "playing", "sonos": sp.player_name, "url": audio_url})
    except Exception as e:
        return jsonify({"error": f"Sonos error: {str(e)}"}), 500


@app.route("/audio/<path:filename>")
def audio_file(filename):
    return Response(
        Path(f"/tmp/orin_audio/{filename}").read_bytes(),
        mimetype="audio/wav",
        headers={
            "Cache-Control": "no-cache",
            "Content-Length": str(Path(f"/tmp/orin_audio/{filename}").stat().st_size),
        },
    )


@app.route("/api/camera/stop", methods=["POST"])
def stop_camera():
    """Allow manually stopping the camera (frees /dev/video0)."""
    _kill_camera()
    return jsonify({"status": "ok"})


@app.route("/api/camera/start", methods=["POST"])
def restart_camera():
    """Manually start the camera (if not already running)."""
    _ensure_camera()
    return jsonify({"status": "ok"})


# ── Shutdown ───────────────────────────────────────────────────────
atexit.register(_kill_camera)

if __name__ == "__main__":
    print(f"  🏠  Orin Home Hub")
    print(f"  📷  Camera → {CAM_DEVICE}")
    print(f"  🔊  Sonos  → auto-discover")
    print(f"  🌐  http://0.0.0.0:{PORT}")
    print(f"  🔒  https://{os.environ.get('TAILSCALE_HOSTNAME', 'ubuntu.tail6609df.ts.net')}")
    _ensure_camera()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
