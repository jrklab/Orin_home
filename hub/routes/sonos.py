"""Sonos page routes.

Handles:
  • GET/POST /api/sonos/volume
  • POST      /api/audio/play   — accept browser blob, play on Sonos
  • GET       /audio/<filename> — serve converted WAV back to Sonos
"""
import subprocess

from flask import Blueprint, Response, jsonify, request
from pathlib import Path

from config import AUDIO_DIR, PORT, SONOS_ENABLED
from sonos_utils import get_speaker, HOST_IP

bp = Blueprint("sonos", __name__)


@bp.route("/api/sonos/volume", methods=["GET", "POST"])
def sonos_volume():
    if not SONOS_ENABLED:
        return jsonify({"error": "Sonos disabled"}), 503
    sp = get_speaker()
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


@bp.route("/api/audio/play", methods=["POST"])
def play_audio_on_sonos():
    """Accept browser audio blob, convert to WAV, play on Sonos."""
    if not SONOS_ENABLED:
        return jsonify({"error": "Sonos disabled"}), 503
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    sp = get_speaker()
    if not sp:
        return jsonify({"error": "Sonos not found"}), 504

    audio_blob = request.files["audio"]
    raw_path = AUDIO_DIR / "input.tmp"
    wav_path = AUDIO_DIR / "latest.wav"
    audio_blob.save(raw_path)

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
        try:
            sp.stop()
        except Exception:
            pass
        audio_url = f"http://{HOST_IP}:{PORT}/audio/latest.wav"
        sp.play_uri(audio_url)
        return jsonify({"status": "playing", "sonos": sp.player_name, "url": audio_url})
    except Exception as e:
        return jsonify({"error": f"Sonos error: {str(e)}"}), 500


@bp.route("/audio/<filename>")
def audio_file(filename):
    safe_name = Path(filename).name   # prevent path traversal
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
