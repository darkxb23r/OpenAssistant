"""
Jarvis's voice from ElevenLabs, when ~/.jarvis/secrets.env has ELEVENLABS_API_KEY.

Used by the browser interface (web.py's /tts) and the local voice layer (voice.py). Anything that
goes wrong (no network, a bad key, the monthly quota used up) falls back to the local Kokoro
voice for that sentence, so Jarvis never goes silent; quota and key errors pause ElevenLabs for
PAUSE_S rather than failing every sentence. Phone calls keep Kokoro.

Every sentence Jarvis says is sent to ElevenLabs, mail it reads out included.

The key is read from secrets.env only, never from the environment: Jarvis's shell commands
inherit its environment, but can't read ~/.jarvis.

Settings (secrets.env):
  ELEVENLABS_API_KEY    required
  ELEVENLABS_VOICE_ID   default George (JBFqnCBsd6RMkjVDRZzb), the original project's voice
  ELEVENLABS_MODEL      default eleven_flash_v2_5 (fast); eleven_multilingual_v2 is richer but slower
"""
import logging
import time

import httpx

import mcp_launch

log = logging.getLogger("jarvis")

API = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"   # George: a warm British male voice
DEFAULT_MODEL = "eleven_flash_v2_5"
OUTPUT = "mp3_44100_128"                 # the default format, on every plan
# The original project's settings: a little expressive, close to the voice, slightly brisk.
VOICE_SETTINGS = {"stability": 0.4, "similarity_boost": 0.75, "speed": 1.05}
TIMEOUT_S = 20
PAUSE_S = 30 * 60
_paused_until = 0.0
_http: httpx.Client | None = None   # kept open: a new TLS handshake per sentence was a pause per sentence


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=TIMEOUT_S, limits=httpx.Limits(keepalive_expiry=120))
    return _http


def settings() -> dict | None:
    """ElevenLabs settings from secrets.env, or None when there's no key."""
    s = mcp_launch.read_secrets()
    if not s.get("ELEVENLABS_API_KEY"):
        return None
    return {"key": s["ELEVENLABS_API_KEY"], "voice": s.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE,
            "model": s.get("ELEVENLABS_MODEL") or DEFAULT_MODEL}


def mp3(text: str, cfg: dict, client: httpx.Client | None = None) -> bytes | None:
    """The sentence as MP3, or None (paused, or it failed) so the caller uses Kokoro."""
    global _paused_until
    if time.monotonic() < _paused_until:
        return None
    try:
        r = (client or _client()).post(API.format(voice=cfg["voice"]), params={"output_format": OUTPUT},
                                       headers={"xi-api-key": cfg["key"], "accept": "audio/mpeg"},
                                       json={"text": text, "model_id": cfg["model"], "voice_settings": VOICE_SETTINGS})
    except httpx.HTTPError as e:
        log.warning("ElevenLabs unreachable, using Kokoro for this sentence: %s", type(e).__name__)
        return None
    if r.status_code == 200 and r.content:
        return r.content
    detail = r.text[:300]
    # A bad key, no credits left, or rate limited: it won't fix itself sentence by sentence.
    if r.status_code in (401, 402, 403, 429) or "quota" in detail.lower():
        _paused_until = time.monotonic() + PAUSE_S
        log.warning("ElevenLabs said %s; using Kokoro for %d minutes: %s", r.status_code, PAUSE_S // 60, detail)
        print(f"   [ElevenLabs refused ({r.status_code}); Jarvis uses its local voice for {PAUSE_S // 60} minutes]")
    else:
        log.warning("ElevenLabs said %s, using Kokoro for this sentence: %s", r.status_code, detail)
    return None


def samples(text: str, cfg: dict, rate: int = 24000, client: httpx.Client | None = None):
    """The sentence as float32 samples at `rate` (for the voice layer's speakers), or None."""
    audio = mp3(text, cfg, client)
    if audio is None:
        return None
    from voice import decode_audio
    try:
        return decode_audio(audio, rate)
    except Exception as e:
        log.warning("couldn't decode ElevenLabs audio, using Kokoro: %s", e)
        return None


def describe(cfg: dict | None) -> str:
    return (f"ElevenLabs ({cfg['model']}, voice {cfg['voice']}); every reply is sent to ElevenLabs"
            if cfg else "Kokoro, on this PC")
