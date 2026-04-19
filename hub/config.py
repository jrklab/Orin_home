"""Orin Home Hub — central configuration."""
import os
from pathlib import Path

# ── Camera ───────────────────────────────────────────────────────────
CAM_DEVICE = os.environ.get("ORIN_CAM", "/dev/video0")
RESOLUTION = {"width": 640, "height": 480, "fps": 10}

# ── Server ───────────────────────────────────────────────────────────
PORT       = 5001
HTTPS_PORT = 5443
HOST       = "0.0.0.0"

# ── Audio temp dir ───────────────────────────────────────────────────
AUDIO_DIR = Path("/tmp/orin_audio")
AUDIO_DIR.mkdir(exist_ok=True)

# ── USB Speakerphone ─────────────────────────────────────────────────
# Anker PowerConf: mic only at 16 kHz, speaker only at 48 kHz.
USB_AUDIO_DEVICE = os.environ.get("ORIN_AUDIO_DEVICE", "Anker PowerConf")
AUDIO_IN_RATE    = 16000
AUDIO_OUT_RATE   = 48000
AUDIO_CHANNELS   = 1
AUDIO_CHUNK      = 1024                                      # ≈ 64 ms at 16 kHz
AUDIO_OUT_CHUNK  = AUDIO_CHUNK * (AUDIO_OUT_RATE // AUDIO_IN_RATE)  # 3072

# ── Sonos ─────────────────────────────────────────────────────────────
SONOS_ENABLED   = True    # re-enabled for Task 2 (Sonos page)
SONOS_CACHE_TTL = 60.0    # seconds between soco.discover() calls
# ── Discord (Task 3) ─────────────────────────────────────
# Set DISCORD_WEBHOOK_URL in the .env file (never hard-code).
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
# ── TLS ──────────────────────────────────────────────────────────────
_HUB_DIR  = Path(__file__).parent
CERT_FILE = _HUB_DIR / "cert.pem"
KEY_FILE  = _HUB_DIR / "key.pem"
