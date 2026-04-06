"""
Orin Home Hub - Smart home control server
- USB webcam MJPEG streaming
- Sonos speaker playback (browser push-to-talk)
"""

import os
import subprocess
import signal
import json
import io
import wave
import tempfile
from pathlib import Path
from flask import Flask, render_template, Response, request, jsonify
import soco

# ── Config ──────────────────────────────────────────────────────────────
CAM_DEVICE = os.environ.get("ORIN_CAM_DEVICE", "/dev/video0")
CAM_RESOLUTION = os.environ.get("ORIN_CAM_RESOLUTION", "1280x720")
CAM_FPS = int(os.environ.get("ORIN_CAM_FPS", "15"))
SONOS_IP = os.environ.get("ORIN_SONOS_IP", None)  # auto-discover if None
HOST = os.environ.get("ORIN_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORIN_PORT", "5001"))

app = Flask(__name__)

# ── Sonos helpers ───────────────────────────────────────────────────────
def get_sonos():
    """Find or return the target Sonos speaker."""
    if SONOS_IP:
        return soco.SoCo(SONOS_IP)
    speakers = soco.discover()
    if not speakers:
        raise RuntimeError("No Sonos speakers found on network")
    return list(speakers)[0]  # first discoverable speaker

# Cache the Sonos object
_sonos = None
def sonos():
    global _sonos
    if _sonos is None:
        _sonos = get_sonos()
    return _sonos

def get_sonos_info():
    try:
        sp = sonos()
        return {
            "name": sp.player_name,
            "ip": sp.ip_address,
            "volume": sp.volume,
            "status": sp.get_current_transport_info().get("current_transport_state", "unknown"),
        }
    except Exception as e:
        return {"error": str(e)}

# ── Camera streaming (MJPEG via ffmpeg) ────────────────────────────────
_camera_proc = None
_camera_lock = __import__("threading").Lock()

def generate_frames():
    """Generate MJPEG frames from the USB camera using ffmpeg."""
    global _camera_proc

    cmd = [
        "ffmpeg",
        "-f", "v4l2",
        "-framerate", str(CAM_FPS),
        "-video_size", CAM_RESOLUTION,
        "-input_format", "mjpeg",
        "-i", CAM_DEVICE,
        "-q:v", "8",
        "-r", str(CAM_FPS),
        "-f", "mjpeg",
        "pipe:1",
    ]
    with _camera_lock:
        _camera_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    stderr_reader = threading.Thread(target=_read_stderr, args=(_camera_proc,), daemon=True)
    stderr_reader.start()
    try:
        buf = b""
        while True:
            chunk = _camera_proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            # MJPEG frames are delimited by JPEG SOI/EOI markers
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9")
            while start != -1 and end != -1 and end > start:
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9")
    finally:
        _stop_camera()

def _stop_camera():
    global _camera_proc
    if _camera_proc and _camera_proc.poll() is None:
        os.killpg(os.getpgid(_camera_proc.pid), signal.SIGTERM)
        try:
            _camera_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(_camera_proc.pid), signal.SIGKILL)
        _camera_proc = None

def _read_stderr(proc):
    """Consume ffmpeg stderr so the pipe doesn't fill up."""
    for line in proc.stderr:
        pass  # discard

import threading

# ── Shutdown handling ──────────────────────────────────────────────
def cleanup_camera():
    """Stop camera process on exit."""
    _stop_camera()

# ── Audio: receive from browser, play on Sonos ─────────────────────────
AUDIO_DIR = Path(__file__).parent / "audio_cache"
AUDIO_DIR.mkdir(exist_ok=True)

@app.route("/api/audio/play", methods=["POST"])
def play_audio_on_sonos():
    """
    Accept an audio blob from the browser (recorded via microphone),
    convert to WAV with ffmpeg (Sonos needs WAV/MP3), and play via HTTP.
    """
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]
    raw_path = AUDIO_DIR / "input.tmp"
    wav_path = AUDIO_DIR / "latest.wav"
    audio_file.save(raw_path)

    # Convert to WAV: mono, 44100 Hz (Sonos-compatible)
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(raw_path),
            "-ac", "1",          # mono
            "-ar", "44100",      # 44.1kHz
            "-acodec", "pcm_s16le",
            str(wav_path),
        ], check=True, capture_output=True, timeout=30)
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"Audio conversion failed: {e.stderr.decode()}"}), 500

    try:
        sp = sonos()
        sp.stop()

        # Sonos streams the file directly from us
        host_addr = request.host.split(":")[0]
        audio_url = f"http://{host_addr}:{PORT}/audio/latest.wav"

        sp.play_uri(audio_url)

        return jsonify({
            "status": "playing",
            "sonos": sp.player_name,
            "url": audio_url,
        })
    except Exception as e:
        return jsonify({"error": f"Sonos error: {str(e)}"}), 500

@app.route("/audio/latest.wav")
def serve_audio():
    """Serve the most recent audio file for Sonos streaming."""
    audio_path = AUDIO_DIR / "latest.wav"
    if not audio_path.exists():
        return "No audio", 404
    with open(audio_path, "rb") as f:
        return Response(
            f.read(),
            mimetype="audio/wav",
            headers={
                "Content-Disposition": "inline; filename=latest.wav",
                "Accept-Ranges": "bytes",
            },
        )

# ── Routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", host=HOST, port=PORT)

@app.route("/stream")
def stream():
    """MJPEG stream from USB camera."""
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )

@app.route("/api/status")
def api_status():
    """Return system and device status."""
    status = {
        "camera": {
            "device": CAM_DEVICE,
            "resolution": CAM_RESOLUTION,
            "fps": CAM_FPS,
        },
        "sonos": get_sonos_info(),
    }
    return jsonify(status)

@app.route("/api/sonos", methods=["GET"])
def api_sonos_info():
    return jsonify(get_sonos_info())

@app.route("/api/sonos/volume", methods=["POST"])
def api_sonos_volume():
    data = request.get_json()
    vol = data.get("volume")
    if vol is None:
        return jsonify({"error": "volume required"}), 400
    try:
        sp = sonos()
        sp.volume = int(vol)
        return jsonify({"volume": sp.volume})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sonos/stop", methods=["POST"])
def api_sonos_stop():
    try:
        sp = sonos()
        sp.stop()
        return jsonify({"status": "stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Main ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"🏠 Orin Home Hub starting on {HOST}:{PORT}")
    print(f"   Camera : {CAM_DEVICE} @ {CAM_RESOLUTION}, {CAM_FPS}fps")
    print(f"   Sonos  : auto-discover (or set ORIN_SONOS_IP)")
    print(f"   URL    : http://192.168.1.75:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
