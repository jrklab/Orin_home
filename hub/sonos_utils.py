"""Sonos speaker discovery with a TTL cache to avoid blocking requests."""
import socket
import threading
import time

from soco import discover

from config import SONOS_CACHE_TTL

_sonos_lock = threading.Lock()
_sonos_speaker = None
_sonos_last_discover: float = 0.0


def get_speaker():
    """Return a cached Sonos speaker, re-discovering only when TTL expires."""
    global _sonos_speaker, _sonos_last_discover
    with _sonos_lock:
        now = time.monotonic()
        if _sonos_speaker is not None and (now - _sonos_last_discover) < SONOS_CACHE_TTL:
            return _sonos_speaker
        speakers = discover(timeout=5)
        _sonos_speaker = list(speakers)[0] if speakers else None
        _sonos_last_discover = now
        return _sonos_speaker


def lan_ip() -> str:
    """Resolve the machine's LAN IP (used to build Sonos play_uri URLs)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


HOST_IP = lan_ip()
