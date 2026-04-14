"""USB camera management.

Starts a single ffmpeg process that outputs raw MJPEG to stdout.
A reader thread parses complete JPEG frames (SOI…EOI) and pushes them
into per-client queues so any number of browser tabs can stream
simultaneously without corrupting each other.
"""
import os
import queue
import signal
import subprocess
import threading

from config import CAM_DEVICE, RESOLUTION

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

        while True:
            start = buf.find(SOI)
            if start == -1:
                buf = b""
                break
            end = buf.find(EOI, start + 2)
            if end == -1:
                buf = buf[start:]
                break
            end += 2
            frame = buf[start:end]
            buf = buf[end:]
            with _subscribers_lock:
                for q in list(_frame_subscribers):
                    try:
                        q.put_nowait(frame)
                    except queue.Full:
                        pass


def start() -> None:
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


def stop() -> None:
    """Force-kill the ffmpeg process group."""
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


def status() -> dict | None:
    """Return camera status dict or None if not running."""
    with _camera_lock:
        proc = _camera_proc
    if proc and proc.poll() is None:
        return {
            "device": CAM_DEVICE,
            "fps": RESOLUTION["fps"],
            "resolution": f'{RESOLUTION["width"]}x{RESOLUTION["height"]}',
            "pid": proc.pid,
        }
    return None


def subscribe() -> queue.Queue:
    """Register a new client queue; start camera if needed."""
    start()
    q: queue.Queue = queue.Queue(maxsize=3)
    with _subscribers_lock:
        _frame_subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    """Remove a client queue."""
    with _subscribers_lock:
        try:
            _frame_subscribers.remove(q)
        except ValueError:
            pass
