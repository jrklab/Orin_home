"""
Orin Voice Assistant — wake phrase → transcribe → Discord + camera → feedback.

State machine:
  IDLE        — buffering 2-second windows and running Whisper to detect wake
  CAPTURING   — wake detected; collecting audio until 2 s of silence
  PROCESSING  — running Whisper on the full utterance + Discord POST + feedback

The mic stream is shared with the conference-audio path via audio.mic_subscribe().
The speaker output is shared via audio.play_wav() → audio.speaker_queue.
The camera still is grabbed from the existing MJPEG subscriber (camera.subscribe()).
"""

import io
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np
import requests
import torch

import audio
import camera
from config import (
    AUDIO_IN_RATE,
    AUDIO_CHUNK,
    DISCORD_WEBHOOK_URL,
)

# ── Constants ─────────────────────────────────────────────────────────
_CLIPS      = Path(__file__).parent / "audio_clips"
_CLIP_WAKE  = str(_CLIPS / "listening.wav")
_CLIP_OK    = str(_CLIPS / "sent.wav")
_CLIP_FAIL  = str(_CLIPS / "failed.wav")

# Whisper: loaded lazily on first use so startup is not blocked
# Use 'base' — already cached at ~/.cache/whisper/base.pt
# Switch to 'small.en' once downloaded (more accurate)
_WHISPER_MODEL_NAME = "base"
_whisper_model = None
_whisper_lock   = threading.Lock()
_WHISPER_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Energy threshold for silence detection (tune if environment is noisy)
_SILENCE_RMS      = 200       # int16 RMS below this = silence
_SILENCE_SECS     = 2.0       # seconds of silence to end utterance
_WAKE_WINDOW_SECS = 2.0       # seconds of audio per Whisper wake-check chunk
_MAX_CAPTURE_SECS = 10.0      # hard cap on capture length after wake
_WAKE_MIN_RMS     = 100        # skip Whisper on near-silent windows (noise floor ~12 on PowerConf)
_WAKE_OVERLAP_SECS = 1.0       # seconds of audio kept as overlap between wake windows

# Wake phrase — both tokens must appear in order
_WAKE_PATTERN = re.compile(r"\bxiao bai\b", re.IGNORECASE)

# Assistant state
_State = type("State", (), {"IDLE": "IDLE", "CAPTURING": "CAPTURING", "PROCESSING": "PROCESSING"})
_state      = _State.IDLE
_state_lock = threading.Lock()


# ── Whisper ───────────────────────────────────────────────────────────

def _get_model():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            import whisper
            print(f"  🤖  Loading Whisper {_WHISPER_MODEL_NAME} on {_WHISPER_DEVICE}…", flush=True)
            _whisper_model = whisper.load_model(_WHISPER_MODEL_NAME, device=_WHISPER_DEVICE)
            print(f"  🤖  Whisper ready ({_WHISPER_DEVICE})", flush=True)
        return _whisper_model


def _transcribe(pcm_int16: np.ndarray, translate: bool = False) -> str:
    """Transcribe a 1-D int16 numpy array at AUDIO_IN_RATE to text.

    translate=False (default, wake detection): forces language="en" so Whisper
      outputs phonetic romanization (e.g. "xiao bai") even when the speaker
      says Chinese — required for the wake-phrase regex to match.
    translate=True (full utterance): task="translate" with auto language
      detection — outputs English regardless of input language; improves
      accuracy for non-native speakers and handles Chinese naturally.
    """
    model = _get_model()
    float32 = pcm_int16.astype(np.float32) / 32768.0
    if translate:
        result = model.transcribe(float32, fp16=(_WHISPER_DEVICE == "cuda"), task="translate")
    else:
        result = model.transcribe(float32, fp16=(_WHISPER_DEVICE == "cuda"), language="en")
    return result.get("text", "").strip()


# ── Discord ───────────────────────────────────────────────────────────

def _send_discord(message: str) -> bool:
    """Send a text message + camera JPEG to the Discord webhook.

    Returns True on success, False on any error.
    """
    if not DISCORD_WEBHOOK_URL:
        print("  🤖  Discord: DISCORD_WEBHOOK_URL not set — skipped", flush=True)
        return False

    # Grab a single JPEG frame from the camera (reuses existing subscriber)
    jpeg_bytes = _grab_camera_frame()

    try:
        files: dict = {
            "payload_json": (None, f'{{"content": {_json_str(message)}}}', "application/json"),
        }
        if jpeg_bytes:
            files["file"] = ("snapshot.jpg", jpeg_bytes, "image/jpeg")

        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            files=files,
            timeout=15,
        )
        if resp.status_code in (200, 204):
            print(f"  🤖  Discord: sent OK (HTTP {resp.status_code})", flush=True)
            return True
        else:
            print(f"  🤖  Discord: HTTP {resp.status_code} — {resp.text[:200]}", flush=True)
            return False
    except Exception as e:
        print(f"  🤖  Discord error: {e}", flush=True)
        return False


def _json_str(s: str) -> str:
    """Minimal JSON string escaping (avoids importing json for a single field)."""
    import json
    return json.dumps(s)


def _grab_camera_frame(timeout: float = 3.0) -> bytes | None:
    """Subscribe to the camera feed, grab one frame, unsubscribe."""
    try:
        q = camera.subscribe()
        frame = q.get(timeout=timeout)
        camera.unsubscribe(q)
        return frame
    except Exception as e:
        print(f"  🤖  Camera grab error: {e}", flush=True)
        return None


# ── Main listener loop ────────────────────────────────────────────────

def _rms(pcm: np.ndarray) -> float:
    return float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))


def _listener_thread() -> None:
    """Continuously read mic chunks, detect wake phrase, capture and dispatch."""
    global _state

    mic_q = audio.mic_subscribe()
    samples_per_window = int(AUDIO_IN_RATE * _WAKE_WINDOW_SECS)
    samples_per_overlap = int(AUDIO_IN_RATE * _WAKE_OVERLAP_SECS)
    samples_per_silence = int(AUDIO_IN_RATE * _SILENCE_SECS)

    wake_buf: list[bytes] = []   # accumulates chunks for wake-word window
    cap_buf:  list[bytes] = []   # accumulates chunks after wake detected
    silence_chunks = 0
    max_cap_chunks = int(_MAX_CAPTURE_SECS * AUDIO_IN_RATE / AUDIO_CHUNK)

    # Transcription runs in a sub-thread so mic collection is never blocked
    _transcribe_pending = threading.Event()
    _transcribe_result: list[str] = []   # shared result slot

    def _do_transcribe_bg(window: np.ndarray) -> None:
        text = _transcribe(window)
        _transcribe_result.clear()
        _transcribe_result.append(text)
        _transcribe_pending.set()

    _transcribe_thread: threading.Thread | None = None

    print("  🤖  Assistant listening…", flush=True)
    _diag_counter = 0  # Print RMS diagnostic periodically

    while True:
        try:
            chunk = mic_q.get(timeout=1.0)
        except queue.Empty:
            continue

        arr = np.frombuffer(chunk, dtype=np.int16)

        with _state_lock:
            state = _state

        if state == _State.IDLE:
            wake_buf.append(chunk)
            total = sum(len(c) // 2 for c in wake_buf)

            # Periodic diagnostic: print RMS every ~5 seconds
            _diag_counter += 1
            if _diag_counter >= int(5 * AUDIO_IN_RATE / AUDIO_CHUNK):
                window_rms = _rms(np.frombuffer(b"".join(wake_buf), dtype=np.int16)) if wake_buf else 0
                busy = _transcribe_thread and _transcribe_thread.is_alive()
                print(f"  🤖  [diag] window_rms={window_rms:.0f}  buf_chunks={len(wake_buf)}"
                      f"  transcribing={busy}", flush=True)
                _diag_counter = 0

            # Check if a background transcription finished
            if _transcribe_pending.is_set():
                _transcribe_pending.clear()
                text = _transcribe_result[0] if _transcribe_result else ""
                print(f"  🤖  [IDLE] heard: {text!r}", flush=True)
                if _WAKE_PATTERN.search(text):
                    print("  🤖  Wake phrase detected!", flush=True)
                    audio.play_wav(_CLIP_WAKE)
                    with _state_lock:
                        _state = _State.CAPTURING
                    cap_buf = []
                    silence_chunks = 0
                    wake_buf = []

            # Kick off a new transcription BEFORE trimming — trigger fires as
            # soon as we have >= samples_per_window samples in the buffer.
            if total >= samples_per_window and (
                _transcribe_thread is None or not _transcribe_thread.is_alive()
            ):
                window = np.frombuffer(b"".join(wake_buf), dtype=np.int16)
                # Clip to exactly samples_per_window in case buffer overshot
                window = window[-samples_per_window:]
                # Keep the last overlap_secs as a prefix for the next window
                # so a wake phrase straddling a window boundary is still caught.
                overlap_bytes = (window[-samples_per_overlap:].astype(np.int16)
                                 .tobytes())
                if _rms(window) < _WAKE_MIN_RMS:
                    wake_buf = [overlap_bytes]
                else:
                    _transcribe_pending.clear()
                    _transcribe_result.clear()
                    _transcribe_thread = threading.Thread(
                        target=_do_transcribe_bg, args=(window,), daemon=True
                    )
                    _transcribe_thread.start()
                    wake_buf = [overlap_bytes]  # seed next window with overlap

            # Trim to keep only the last window worth of audio
            while total > samples_per_window and wake_buf:
                total -= len(wake_buf[0]) // 2
                wake_buf.pop(0)

        elif state == _State.CAPTURING:
            cap_buf.append(chunk)
            if _rms(arr) < _SILENCE_RMS:
                silence_chunks += 1
            else:
                silence_chunks = 0

            silent_enough = (silence_chunks * AUDIO_CHUNK) >= samples_per_silence
            too_long      = len(cap_buf) >= max_cap_chunks

            if silent_enough or too_long:
                with _state_lock:
                    _state = _State.PROCESSING
                captured = list(cap_buf)
                cap_buf = []
                threading.Thread(
                    target=_process_utterance,
                    args=(captured,),
                    daemon=True,
                ).start()

        # While PROCESSING, mic chunks are just dropped (state returns to IDLE
        # when _process_utterance() finishes).


def _process_utterance(chunks: list[bytes]) -> None:
    """Transcribe captured audio, send to Discord, play feedback."""
    global _state

    try:
        pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
        print(f"  🤖  Transcribing {len(pcm)/AUDIO_IN_RATE:.1f}s of audio…", flush=True)
        text = _transcribe(pcm, translate=True)
        print(f"  🤖  Full text: {text!r}", flush=True)

        # Strip the wake phrase prefix to get just the message
        clean = _WAKE_PATTERN.sub("", text).strip().strip(",. ")
        # Also strip common action prefixes
        for prefix in ("call my dad", "send my dad a message", "send a message", "call"):
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):].strip().strip(",. ")
                break

        message = f"📞 Message from Orin: {text}"
        if clean:
            message = f"📞 Message from Orin: {clean}"

        ok = _send_discord(message)
        audio.play_wav(_CLIP_OK if ok else _CLIP_FAIL)

    except Exception as e:
        print(f"  🤖  Process error: {e}", flush=True)
        audio.play_wav(_CLIP_FAIL)
    finally:
        with _state_lock:
            _state = _State.IDLE
        print("  🤖  Assistant back to listening", flush=True)


# ── Public API ────────────────────────────────────────────────────────

def start() -> None:
    """Start the assistant background thread. Call once at app startup."""
    # Pre-load Whisper in a separate thread so it's ready before first trigger
    threading.Thread(target=_get_model, daemon=True, name="whisper-preload").start()
    threading.Thread(target=_listener_thread, daemon=True, name="assistant").start()
