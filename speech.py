"""
Jarvis's ears and voice as a service, for any client that can't do its own: the browser
interface (web.py's /stt and /tts), phone calls (phone.py), and voice satellites.

- Hearing: the voice layer's Whisper (large-v3-turbo on a GPU, small.en on the CPU). A recorded
  segment (WebM/Opus from a browser, WAV, MP3...) is decoded to 16 kHz with voice.decode_audio.
- Speaking: ElevenLabs when ~/.jarvis/secrets.env has a key and it answers (elevenlabs.py), else
  the voice layer's Kokoro voice. Paths, links and dates are said the way the voice layer says them.

Both models load once, on first use or in the background at startup (warm()), and are used one
call at a time.
"""
import importlib.util
import io
import logging
import threading
import wave

import elevenlabs
import identity

log = logging.getLogger("jarvis.speech")
HINT_LIMIT = 30   # Whisper's hint words share its prompt; too many crowd out what was said
STT_AVAILABLE = importlib.util.find_spec("faster_whisper") is not None
TTS_AVAILABLE = importlib.util.find_spec("kokoro_onnx") is not None


class Speech:
    def __init__(self, spoken_paths: dict[str, str] | None = None, enabled: bool = True, hints=None):
        self.hears = enabled and STT_AVAILABLE                    # /stt works
        self._hints = hints or (lambda: [])   # names the user says (projects, machines), for Whisper
        self.eleven = elevenlabs.settings() if enabled else None   # Jarvis's voice from ElevenLabs
        self.speaks = enabled and (TTS_AVAILABLE or self.eleven is not None)   # /tts works
        self._spoken_paths = spoken_paths
        self._stt = None                # the Whisper model
        self._stt_lock = threading.Lock()
        self._tts = None                # the Kokoro voice
        self._tts_lock = threading.Lock()

    def describe_voice(self) -> str:
        return elevenlabs.describe(self.eleven)

    def warm(self, loop) -> None:
        """Load the models in the background, so the first "Hey Jarvis" isn't waiting on them."""
        if self.hears:
            loop.run_in_executor(None, self.whisper)
        if self.speaks and TTS_AVAILABLE:   # Kokoro: the voice, or the fallback when ElevenLabs fails
            loop.run_in_executor(None, self.kokoro)

    def whisper(self):
        with self._stt_lock:
            if self._stt is None:
                from voice import _load_whisper
                self._stt, name = _load_whisper()
                print(f"   [speech-to-text: {name}]")
            return self._stt

    def kokoro(self):
        with self._tts_lock:
            if self._tts is None:
                from voice import _download_kokoro, _load_tts
                _download_kokoro()
                self._tts, name = _load_tts()
                print(f"   [text-to-speech: {name}]")
            return self._tts

    def transcribe(self, audio: bytes) -> str:
        """A recorded segment as text ("" when there's nothing in it)."""
        from voice import decode_audio
        samples = decode_audio(audio, 16000)
        if not samples.size:
            return ""
        model = self.whisper()
        with self._stt_lock:
            # Hint words: its name (identity.py) and "Jarvis" (on its own, a short "Hey Jarvis" often comes back as "Javis"), and
            # the user's project and machine names, which Whisper otherwise splits ("Reclaimer Forge").
            # A beam search instead of greedy decoding: slower by a fraction, noticeably more accurate.
            segments, _ = model.transcribe(samples, language="en", beam_size=5, hotwords=self.hotwords(),
                                           condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
        log.info("heard %.1fs: %r", samples.size / 16000, text)
        return text

    def hotwords(self) -> str:
        try:
            names = list(dict.fromkeys(n for n in self._hints() if n))[:HINT_LIMIT]
        except Exception:
            names = []
        called = list(dict.fromkeys([identity.NAME, identity.DEFAULT_NAME]))   # its name, and Jarvis
        return ", ".join([*called, *names])

    def speak(self, text: str) -> tuple[bytes, str]:
        """One sentence in Jarvis's voice, as (audio, media type): ElevenLabs' MP3, else a Kokoro WAV."""
        from voice import LANG, VOICE, _clean_for_speech, path_names
        spoken = _clean_for_speech(text, path_names(self._spoken_paths)) or text
        if self.eleven and (audio := elevenlabs.mp3(spoken, self.eleven)):
            return audio, "audio/mpeg"
        if not TTS_AVAILABLE:
            raise RuntimeError("ElevenLabs didn't answer and kokoro-onnx isn't installed")
        kokoro = self.kokoro()
        with self._tts_lock:
            samples, rate = kokoro.create(spoken, voice=VOICE, lang=LANG)
        import numpy as np
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
        return out.getvalue(), "audio/wav"

    def call_models(self):
        """(Whisper, Kokoro) for phone calls, which do their own audio handling."""
        return self.whisper(), self.kokoro()
