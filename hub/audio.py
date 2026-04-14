"""USB speakerphone management (Anker PowerConf).

Two separate sounddevice streams:
  • InputStream  @ AUDIO_IN_RATE  (16 kHz) — mic → Socket.IO broadcast
  • OutputStream @ AUDIO_OUT_RATE (48 kHz) — Socket.IO chunks → speaker
    Browser sends 16 kHz PCM int16; output stream upsamples 3× by repeat.
"""
import queue
import threading

from config import (
    AUDIO_CHANNELS,
    AUDIO_CHUNK,
    AUDIO_IN_RATE,
    AUDIO_OUT_CHUNK,
    AUDIO_OUT_RATE,
    USB_AUDIO_DEVICE,
)
from extensions import socketio

try:
    import numpy as np
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except (ImportError, OSError):
    HAS_SOUNDDEVICE = False

_mic_queue: queue.Queue = queue.Queue(maxsize=20)
speaker_queue: queue.Queue = queue.Queue(maxsize=20)   # public — routes write here

_audio_lock = threading.Lock()
_mic_stream = None
_speaker_stream = None


def _audio_input_callback(indata, frames, time_info, status) -> None:
    try:
        _mic_queue.put_nowait(bytes(indata))
    except queue.Full:
        pass


def _audio_output_callback(outdata, frames, time_info, status) -> None:
    upsample = AUDIO_OUT_RATE // AUDIO_IN_RATE  # 3
    try:
        data = speaker_queue.get_nowait()
        arr = np.frombuffer(data, dtype=np.int16)
        upsampled = np.repeat(arr, upsample)
        if len(upsampled) >= frames:
            outdata[:] = upsampled[:frames].reshape((-1, AUDIO_CHANNELS))
        else:
            outdata.fill(0)
            outdata[: len(upsampled)] = upsampled.reshape((-1, AUDIO_CHANNELS))
    except queue.Empty:
        outdata.fill(0)
    except Exception:
        outdata.fill(0)


def _mic_broadcast_thread() -> None:
    """Drain mic queue and emit each chunk to all Socket.IO clients."""
    while True:
        data = _mic_queue.get()
        socketio.emit("audio_chunk", data)


def start() -> None:
    global _mic_stream, _speaker_stream
    if not HAS_SOUNDDEVICE:
        print("    Audio skipped   — sounddevice / PortAudio not installed", flush=True)
        return
    with _audio_lock:
        try:
            _mic_stream = sd.InputStream(
                device=USB_AUDIO_DEVICE,
                samplerate=AUDIO_IN_RATE,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                blocksize=AUDIO_CHUNK,
                callback=_audio_input_callback,
            )
            _mic_stream.start()
            print(f"    Mic stream OK   → device='{USB_AUDIO_DEVICE}' {AUDIO_IN_RATE} Hz", flush=True)
        except Exception as e:
            print(f"    Mic stream error: {e}", flush=True)
        try:
            _speaker_stream = sd.OutputStream(
                device=USB_AUDIO_DEVICE,
                samplerate=AUDIO_OUT_RATE,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                blocksize=AUDIO_OUT_CHUNK,
                callback=_audio_output_callback,
            )
            _speaker_stream.start()
            print(f"    Speaker OK      → device='{USB_AUDIO_DEVICE}' {AUDIO_OUT_RATE} Hz", flush=True)
        except Exception as e:
            print(f"    Speaker stream error: {e}", flush=True)
    threading.Thread(target=_mic_broadcast_thread, daemon=True).start()


def stop() -> None:
    global _mic_stream, _speaker_stream
    with _audio_lock:
        for s in (_mic_stream, _speaker_stream):
            if s:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        _mic_stream = None
        _speaker_stream = None
