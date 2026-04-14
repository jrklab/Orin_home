"""Conference page routes + Socket.IO event handlers.

Handles:
  • GET  /          → render index.html (the full SPA shell)
  • GET  /stream    → MJPEG camera feed
  • GET  /api/status
  • POST /api/camera/stop
  • POST /api/camera/start
  • Socket.IO: connect / disconnect / browser_audio
"""
import queue

from flask import Blueprint, Response, jsonify, render_template, request

import camera
import audio
from config import CAM_DEVICE, HTTPS_PORT, PORT, RESOLUTION
from extensions import socketio

bp = Blueprint("conference", __name__)


@bp.route("/")
def index():
    host = request.host.split(":")[0]
    return render_template("index.html", host=host, port=PORT, https_port=HTTPS_PORT)


@bp.route("/stream")
def stream():
    """MJPEG stream — each client gets its own frame queue."""
    client_q = camera.subscribe()

    def generate_frames():
        try:
            while True:
                try:
                    frame = client_q.get(timeout=5)
                except queue.Empty:
                    camera.start()   # restart ffmpeg if it died
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
            camera.unsubscribe(client_q)

    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=--frame",
    )


@bp.route("/api/status")
def api_status():
    resp = {
        "camera": camera.status(),
        "sonos": None,
        "audio": {
            "device": "Anker PowerConf",
            "in_rate": 16000,
            "out_rate": 48000,
        },
    }
    # Sonos status (non-blocking — uses cache)
    try:
        from sonos_utils import get_speaker
        speaker = get_speaker()
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


@bp.route("/api/camera/stop", methods=["POST"])
def stop_camera():
    camera.stop()
    return jsonify({"status": "ok"})


@bp.route("/api/camera/start", methods=["POST"])
def start_camera():
    camera.start()
    return jsonify({"status": "ok"})


# ── Socket.IO events ─────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    print(f"    WS connect      sid={request.sid}", flush=True)


@socketio.on("disconnect")
def on_disconnect():
    print(f"    WS disconnect   sid={request.sid}", flush=True)


@socketio.on("browser_audio")
def on_browser_audio(data):
    """Forward browser mic PCM to the USB speakerphone output queue."""
    try:
        audio.speaker_queue.put_nowait(data)
    except Exception:
        pass
