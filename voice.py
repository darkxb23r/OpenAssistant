"""
Jarvis, phase 2: the voice layer.

Mic -> "Hey Jarvis" wake word (openWakeWord) -> record until you stop talking (Silero VAD)
-> speech-to-text (faster-whisper, on the GPU when CUDA is available) -> Jarvis.
Jarvis's replies -> text-to-speech (Kokoro, on the GPU with onnxruntime-gpu) -> speakers. Saying the wake word while Jarvis
is talking cuts it off and starts listening. After Jarvis finishes talking, the mic stays open
for a few seconds so you can reply without the wake word. The next line is synthesized while
the current one plays, and each line is printed as it starts playing.

Everything runs locally by default. Models are downloaded into ./models on first run. A voice
satellite (satellite/satellite.py) keeps the wake word and VAD here and hands hearing and speaking
to the core (RemoteSTT, RemoteTTS).
"""
import asyncio
import os
import re
import urllib.request
from pathlib import Path

import numpy as np

PORTAUDIO_MISSING = (
    "The voice layer needs PortAudio, and this machine doesn't have it. Install it "
    "(Debian/Ubuntu: sudo apt install libportaudio2; Fedora: sudo dnf install portaudio; "
    "Arch: sudo pacman -S portaudio), or run with --text or --core instead.")


class _SoundDevice:
    """sounddevice, imported on first use. It needs PortAudio, which a Linux machine may not have
    installed; the speech helpers below (dates, paths) and phone calls don't need it."""

    _module = None

    def __getattr__(self, name):
        if _SoundDevice._module is None:
            try:
                import sounddevice
            except OSError as e:
                raise SystemExit(PORTAUDIO_MISSING) from e
            _SoundDevice._module = sounddevice
        return getattr(_SoundDevice._module, name)


sd = _SoundDevice()

MODELS = Path(__file__).parent / "models"
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")

RATE = 16000            # mic sample rate expected by openWakeWord, Silero and Whisper
FRAME = 1280            # 80 ms frames, what openWakeWord expects
WAKE_THRESHOLD = 0.5
SPEECH_THRESHOLD = 0.5
END_SILENCE_S = 0.8     # this much quiet after speech ends your turn
NO_SPEECH_TIMEOUT_S = 5   # also how long the mic stays open for a follow-up after Jarvis speaks
MAX_UTTERANCE_S = 20
VOICE, LANG = "bm_lewis", "en-gb"

def _download_models(kokoro: bool = True) -> None:
    MODELS.mkdir(exist_ok=True)
    if kokoro:   # not for a remote voice (a satellite): the core speaks
        _download_kokoro()
    import openwakeword.utils
    openwakeword.utils.download_models(["hey_jarvis"])


def _download_kokoro() -> None:
    MODELS.mkdir(exist_ok=True)
    for name in KOKORO_FILES:
        path = MODELS / name
        if not path.exists():
            print(f"   [downloading {name}...]")
            urllib.request.urlretrieve(KOKORO_URL + name, path.with_suffix(".part"))
            path.with_suffix(".part").rename(path)


def decode_audio(audio: bytes, rate: int):
    """Encoded audio (WebM/Ogg Opus, MP3, MP4...) as mono float32 at `rate`: 16 kHz for Whisper,
    24 kHz for the speakers. Decoded with PyAV (a faster-whisper dependency) rather than by
    faster-whisper itself, whose decoder breaks with newer PyAV releases."""
    import io
    import av

    chunks = []
    with av.open(io.BytesIO(audio)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return np.zeros(0, np.float32)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
        for frame in container.decode(stream):
            chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]
        chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)


def path_names(spoken_paths: dict[str, str] | None) -> list[tuple[list[str], str]]:
    """Folders and their spoken names, for _speak_path. Longest folder first, so the workspace
    wins over the home folder that contains it."""
    return sorted(
        (([part.lower() for part in re.split(r"[\\/]+", folder) if part], name)
         for folder, name in (spoken_paths or {}).items()),
        key=lambda entry: -len(entry[0]),
    )


def _add_cuda_dlls() -> None:
    """faster-whisper finds cuBLAS/cuDNN from the nvidia-* pip wheels only if told where they are.
    Windows: add their bin folders to the DLL search path. Linux: the loader only reads
    LD_LIBRARY_PATH at startup, so load the libraries now (cuBLASLt first, which cuBLAS needs);
    ctranslate2's later dlopen by name then finds them already loaded."""
    try:
        import nvidia
    except ImportError:
        return
    for root in nvidia.__path__:
        for lib in ("cublas", "cudnn"):
            if os.name == "nt":
                bin_dir = os.path.join(root, lib, "bin")
                if os.path.isdir(bin_dir):
                    os.add_dll_directory(bin_dir)
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
                continue
            lib_dir = os.path.join(root, lib, "lib")
            if not os.path.isdir(lib_dir):
                continue
            import ctypes
            for name in sorted(os.listdir(lib_dir), key=lambda n: (not n.startswith("libcublasLt"), n)):
                if ".so" in name:
                    try:
                        ctypes.CDLL(os.path.join(lib_dir, name), mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass   # no NVIDIA driver, or a library this build doesn't need: Whisper falls back to the CPU


def _load_whisper():
    from faster_whisper import WhisperModel

    _add_cuda_dlls()
    import ctranslate2
    # Ask first: without a usable GPU, trying would download the 1.6 GB large model only to fail.
    if ctranslate2.get_cuda_device_count() == 0:
        return WhisperModel("small.en", device="cpu", compute_type="int8"), "small.en on CPU"
    try:
        model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
        list(model.transcribe(np.zeros(RATE, np.float32), language="en")[0])  # fails here if CUDA libs are missing
        return model, "large-v3-turbo on GPU"
    except Exception:
        return WhisperModel("small.en", device="cpu", compute_type="int8"), "small.en on CPU"


# A file path in a reply: starts with a drive (C:\), ~\, a network share (\\host), or /dir/.
PATH_TOKEN = re.compile(r"(?<![\w~])(?:[A-Za-z]:[\\/]|~(?=[\\/])|\\\\(?=[\w.-])|/(?=[\w.-]+/))\S*")


def _speak_path(token: str, path_names: list[tuple[list[str], str]]) -> str:
    """Say a path as its folders, separated by short pauses, with well-known folders named:
    "C:\\Users\\me\\jarvis-workspace\\proj" -> "the workspace, proj"."""
    path, tail = re.fullmatch(r"(.*?)([.,;:!?)\]'\"]*)", token, re.DOTALL).groups()
    parts = [part for part in re.split(r"[\\/]+", path) if part]
    for prefix, name in path_names:
        if [part.lower() for part in parts[:len(prefix)]] == prefix:
            parts = [name] + parts[len(prefix):]
            break
    else:
        if re.match(r"[A-Za-z]:", path):
            parts[0] = f"{path[0].upper()} drive"
        elif path.startswith("/"):
            parts.insert(0, "root")
    return ", ".join(parts) + tail


def _cuda_device() -> bool:
    """Whether there's an NVIDIA GPU to use (asked through ctranslate2, which Whisper brings). Without
    it, onnxruntime-gpu still lists its CUDA and TensorRT providers, and trying them fails noisily."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return True   # can't tell: try the GPU, and fall back as before


def _load_tts():
    """Kokoro on the GPU when onnxruntime-gpu can reach CUDA, otherwise on the CPU, by name: with
    onnxruntime-gpu installed and no GPU, kokoro-onnx's own default tries TensorRT and CUDA first and
    fails to load at all."""
    import onnxruntime as rt
    from kokoro_onnx import Kokoro

    model, voices = str(MODELS / KOKORO_FILES[0]), str(MODELS / KOKORO_FILES[1])
    options = rt.SessionOptions()
    options.log_severity_level = 3  # errors only; its GPU warnings about this model are harmless
    if "CUDAExecutionProvider" in rt.get_available_providers() and _cuda_device():
        try:
            rt.preload_dlls()  # CUDA 13 and cuDNN from the nvidia-* pip wheels
            session = rt.InferenceSession(
                model, options, providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
            )
            if session.get_providers()[0] == "CUDAExecutionProvider":
                return Kokoro.from_session(session, voices), "Kokoro on GPU"
            print("   [CUDA isn't available to onnxruntime, so text-to-speech uses the CPU]")
        except Exception as e:
            print(f"   [GPU text-to-speech failed, using the CPU: {e}]")
    cpu = rt.InferenceSession(model, options, providers=["CPUExecutionProvider"])
    return Kokoro.from_session(cpu, voices), "Kokoro on CPU"


# --- speech engines ------------------------------------------------------------------------------
# Voice takes its speech-to-text and text-to-speech as engines with the local models' own
# interfaces: Whisper's transcribe(audio, language=, beam_size=) -> (segments, info), and Kokoro's
# async create_stream(text, voice=, lang=) -> (samples, rate) chunks. The local models are the
# default; the remote engines below send the work to a Jarvis core's /stt and /tts instead (a
# voice satellite), with the media ticket as a bearer token.
class RemoteSpeech:
    """The core's /stt and /tts, at base_url ("http://127.0.0.1:8790"). The ticket comes with each
    welcome (protocol.py) and is set here; on_refused() is called when the core turns it down
    (expired, or the device was revoked), so the client can say hello again for a new one."""

    MAX_CHARS = 1800   # /tts takes up to 2000 characters at a time

    def __init__(self, base_url: str, timeout_s: float = 60):
        import httpx   # only remote speech needs it
        self.base_url = base_url.rstrip("/")
        self.ticket: str | None = None
        self.on_refused = None
        self._http = httpx.Client(timeout=timeout_s, trust_env=False)

    def _post(self, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        if self.ticket:
            headers["authorization"] = f"Bearer {self.ticket}"
        response = self._http.post(self.base_url + path, headers=headers, **kwargs)
        if response.status_code == 403:
            if self.on_refused:
                self.on_refused()
            raise PermissionError(f"the core refused the media ticket for {path}")
        response.raise_for_status()
        return response

    def close(self) -> None:
        self._http.close()


class _Segment:
    def __init__(self, text: str):
        self.text = text


class RemoteSTT:
    """Speech-to-text by the core: the recorded utterance goes to /stt as a 16 kHz WAV."""

    name = "the core's speech-to-text"

    def __init__(self, remote: RemoteSpeech):
        self.remote = remote

    def transcribe(self, audio, language: str = "en", beam_size: int = 1, **_):
        import io
        import wave
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        try:
            text = self.remote._post("/stt", content=out.getvalue(), headers={"content-type": "audio/wav"}).json()["text"]
        except Exception as e:   # the core is away or refused: this utterance is lost, say so on screen
            print(f"   [speech-to-text failed: {e}]")
            text = ""
        return [_Segment(str(text or ""))], None


class RemoteTTS:
    """Text-to-speech by the core: each line goes to /tts, which answers in Jarvis's voice (a
    Kokoro WAV, or ElevenLabs' MP3). The core says paths and dates its own way, with its own folder
    names, so Voice leaves the text as it is (cleans_text)."""

    name = "the core's voice"
    cleans_text = True

    def __init__(self, remote: RemoteSpeech):
        self.remote = remote

    def _speak(self, text: str):
        response = self.remote._post("/tts", json={"text": text})
        kind = response.headers.get("content-type", "")
        if kind.startswith("audio/wav"):
            import io
            import wave
            with wave.open(io.BytesIO(response.content)) as w:
                rate, frames = w.getframerate(), w.readframes(w.getnframes())
            return np.frombuffer(frames, "<i2").astype(np.float32) / 32768, rate
        return decode_audio(response.content, 24000), 24000   # ElevenLabs' MP3 (needs PyAV)

    async def create_stream(self, text: str, voice: str = VOICE, lang: str = LANG):
        for part in _chunks(text, RemoteSpeech.MAX_CHARS):
            samples, rate = await asyncio.to_thread(self._speak, part)
            if samples.size:
                yield samples, rate


def _chunks(text: str, limit: int) -> list[str]:
    """A long line in pieces of at most `limit` characters, split between sentences where it can."""
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > limit:
            parts, sentence = parts + ([current] if current else []) + [sentence[:limit]], sentence[limit:]
            current = ""
        if current and len(current) + 1 + len(sentence) > limit:
            parts, current = parts + [current], sentence
        else:
            current = f"{current} {sentence}".strip()
    return parts + ([current] if current else [])


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTH_ABBR = {m[:3].lower(): m for m in MONTHS} | {"sept": "September"}
_DAY_ABBR = {d[:3].lower(): d for d in DAYS} | {"tues": "Tuesday", "thur": "Thursday", "thurs": "Thursday"}
_MONTH_WORD = "|".join(MONTHS + [m.title() for m in _MONTH_ABBR])
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Only in a date ("Nov 11", "11 Nov", "Wed, Nov"), so names like Jan and words like wed stay.
MONTH_ABBR = re.compile(r"\b(?:(?<=\d )|(?<=\d))(%s)\.?(?!\w)|\b(%s)\.?(?=\s+\d{1,2}(?!\d))"
                        % ("|".join(_MONTH_ABBR), "|".join(_MONTH_ABBR)), re.IGNORECASE)
DAY_ABBR = re.compile(r"\b(%s)\.?(?=,?\s+(?:\d|the\b|%s))" % ("|".join(_DAY_ABBR), _MONTH_WORD), re.IGNORECASE)
MONTH_DAY = re.compile(r"\b(%s)\s+(\d{1,2})\b(?![:\d]|st|nd|rd|th)" % "|".join(MONTHS))


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _speak_dates(text: str) -> str:
    """Dates as they're said: "Wed, Nov 11" -> "Wednesday, November 11th", "2026-11-11" ->
    "the 11th of November 2026"."""
    def iso(m):
        year, month, day = int(m[1]), int(m[2]), int(m[3])
        return f"the {_ordinal(day)} of {MONTHS[month - 1]} {year}" if 1 <= month <= 12 and 1 <= day <= 31 else m[0]

    text = ISO_DATE.sub(iso, text)
    text = MONTH_ABBR.sub(lambda m: _MONTH_ABBR[(m[1] or m[2]).lower()], text)
    text = DAY_ABBR.sub(lambda m: _DAY_ABBR[m[1].lower()], text)
    return MONTH_DAY.sub(lambda m: f"{m[1]} {_ordinal(int(m[2]))}", text)


def _clean_for_speech(text: str, path_names: list[tuple[list[str], str]]) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # [label](url) -> label
    text = re.sub(r"https?://\S+", "a link", text)
    text = PATH_TOKEN.sub(lambda m: _speak_path(m[0], path_names), text)
    text = _speak_dates(text)
    return re.sub(r"[*_`#>|]+", "", text).strip()


def _beep() -> None:
    t = np.arange(int(0.12 * 24000)) / 24000
    sd.play((0.2 * np.sin(2 * np.pi * 880 * t) * np.hanning(t.size)).astype(np.float32), 24000)


class Voice:
    """Async voice I/O: hear() returns what you said, say() queues speech, ask() asks a yes/no question.

    spoken_paths maps folders to short spoken names, like {"C:\\Users\\me": "home"}. Speech
    only: the screen always shows the full path.

    stt and tts are the speech engines (see "speech engines" above): the local Whisper and Kokoro
    (with ElevenLabs) when not given, or e.g. RemoteSTT / RemoteTTS for a satellite. The wake word
    and the voice-activity detector are always local.
    """

    standby = False   # standby: only "Hey Jarvis" opens the mic (jarvis.py switches it)
    _eleven = None    # ElevenLabs settings when it's Jarvis's voice (elevenlabs.py), set in __init__
    on_barge_in = None   # called when the wake word cuts Jarvis off (a satellite tells the core)

    def __init__(self, spoken_paths: dict[str, str] | None = None, stt=None, tts=None) -> None:
        sd.InputStream   # no PortAudio: say so now, before minutes of loading models
        from openwakeword.model import Model
        from openwakeword.vad import VAD

        _download_models(kokoro=tts is None)
        if tts is None:
            self._tts, tts_name = _load_tts()
            self._tts.create("Ready.", voice=VOICE, lang=LANG)  # warm up, so the first real line is quick
            import elevenlabs
            self._eleven = elevenlabs.settings()   # Jarvis's voice from ElevenLabs; Kokoro is the fallback
            print(f"   [text-to-speech: {elevenlabs.describe(self._eleven) if self._eleven else tts_name}]")
        else:
            self._tts = tts
            print(f"   [text-to-speech: {getattr(tts, 'name', type(tts).__name__)}]")
        self._wake = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        self._vad = VAD()
        if stt is None:
            self._stt, stt_name = _load_whisper()
        else:
            self._stt, stt_name = stt, getattr(stt, "name", type(stt).__name__)
        print(f"   [speech-to-text: {stt_name}]")

        self._frames: asyncio.Queue = asyncio.Queue()
        self._path_names = path_names(spoken_paths)
        self._speech: asyncio.Queue = asyncio.Queue()           # (text, show) lines to synthesize
        self._audio: asyncio.Queue = asyncio.Queue(maxsize=4)   # synthesized chunks waiting to play
        self._lines = 0   # lines queued, being synthesized, or playing
        self._in_flight = None   # (text, show) of a line being synthesized that has no audio queued yet
        self._speech_generation = 0   # bumped to cancel whatever is being said
        self._follow_up = False       # open the mic without the wake word once Jarvis stops talking
        self._mic_free = asyncio.Event()   # cleared while you're talking, so Jarvis waits its turn
        self._mic_free.set()
        self._mic = None
        self._tasks = []

    async def __aenter__(self):
        loop = asyncio.get_running_loop()

        def on_audio(indata, frames, time, status):
            loop.call_soon_threadsafe(self._frames.put_nowait, indata[:, 0].copy())

        self._mic = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, callback=on_audio)
        self._mic.start()
        self._tasks = [asyncio.create_task(self._synthesizer()), asyncio.create_task(self._player())]
        return self

    async def __aexit__(self, *exc):
        for task in self._tasks:
            task.cancel()
        self._mic.stop()
        self._mic.close()
        sd.stop()

    # --- the three calls jarvis.py uses -------------------------------------------------

    def say(self, text: str) -> None:
        """Queue a line. It's printed when it starts playing, so screen and speech stay together."""
        self._lines += 1
        self._speech.put_nowait((text, True))

    def ask(self, summary: str, detail: str) -> None:
        print(f"\n   Jarvis wants to {summary}\n   {detail}")
        self._lines += 1
        how = "Say Hey Jarvis, then yes, no, or always." if self.standby else "Yes, no, or always."
        self._speech.put_nowait((f"May I {summary}? {how}", False))

    async def hear(self) -> str:
        while True:
            follow_up = await self._wait_for_turn()
            text = await self._record_and_transcribe(follow_up)
            if text:
                print(f"you (voice)> {text}")
                return text

    # --- internals ------------------------------------------------------------------------

    def _busy_speaking(self) -> bool:
        return self._lines > 0

    def _interrupt(self) -> None:
        """Stop talking. Lines that won't be spoken are still printed, so nothing is lost."""
        self._speech_generation += 1
        while not self._audio.empty():   # in the order they would have played
            _, text, show, _, _ = self._audio.get_nowait()
            if text and show:
                print(f"jarvis (not spoken)> {text}")
        if self._in_flight and self._in_flight[1]:
            print(f"jarvis (not spoken)> {self._in_flight[0]}")
        self._in_flight = None
        while not self._speech.empty():
            text, show = self._speech.get_nowait()
            if show:
                print(f"jarvis (not spoken)> {text}")
        self._lines = 0
        sd.stop()

    async def _wait_for_turn(self) -> bool:
        """Return when you say the wake word (False), or when Jarvis has just finished talking (True)."""
        while True:
            frame = await self._frames.get()
            if self._follow_up and not self._busy_speaking():
                follow_up = True
                break
            if self._wake.predict(frame)["hey_jarvis"] >= WAKE_THRESHOLD:
                if self.on_barge_in and self._busy_speaking():
                    self.on_barge_in()
                self._interrupt()
                follow_up = False
                break
        self._follow_up = False
        self._wake.reset()
        while not self._frames.empty():   # don't transcribe the wake word itself
            self._frames.get_nowait()
        _beep()
        return follow_up

    async def _record_and_transcribe(self, follow_up: bool) -> str:
        """Record until you stop talking. Jarvis holds new speech once you start, so the mic
        never records it. In a follow-up you haven't started yet, Jarvis's next line wins."""
        self._vad.reset_states()
        chunks, heard, quiet, elapsed = [], False, 0.0, 0.0
        if not follow_up:
            self._mic_free.clear()   # you called Jarvis by name, so it waits for you
        try:
            while elapsed < MAX_UTTERANCE_S:
                frame = await self._frames.get()
                chunks.append(frame)
                elapsed += FRAME / RATE
                if self._vad.predict(frame) >= SPEECH_THRESHOLD:
                    heard, quiet = True, 0.0
                    self._mic_free.clear()
                else:
                    quiet += FRAME / RATE
                if heard and quiet >= END_SILENCE_S:
                    break
                if not heard and (elapsed >= NO_SPEECH_TIMEOUT_S or (follow_up and self._busy_speaking())):
                    return ""
        finally:
            self._mic_free.set()
        audio = np.concatenate(chunks).astype(np.float32) / 32768

        def transcribe():
            segments, _ = self._stt.transcribe(audio, language="en", beam_size=1)
            return "".join(s.text for s in segments).strip()

        text = await asyncio.to_thread(transcribe)
        return re.sub(r"^\W*hey,?\s+jarvis\W*", "", text, flags=re.I).strip()

    async def _synthesizer(self) -> None:
        """Turn queued lines into audio ahead of playback, so one line follows the next without a
        pause. Each line ends with a (generation, None, show, None, None) marker."""
        while True:
            text, show = await self._speech.get()
            generation = self._speech_generation
            self._in_flight = (text, show)
            first = True
            try:
                # A remote voice (the core) says paths and dates itself, with its own folder names.
                spoken = text if getattr(self._tts, "cleans_text", False) else _clean_for_speech(text, self._path_names)
                cloud = None
                if self._eleven:   # the whole line at once; Kokoro below if ElevenLabs didn't answer
                    import elevenlabs
                    cloud = await asyncio.to_thread(elevenlabs.samples, spoken, self._eleven, 24000)
                if cloud is not None and cloud.size:
                    if generation == self._speech_generation:
                        await self._audio.put((generation, text, show, cloud, 24000))
                        self._in_flight = None  # the player prints it from here
                else:
                    async for samples, rate in self._tts.create_stream(spoken, voice=VOICE, lang=LANG):
                        if generation != self._speech_generation:
                            break
                        await self._audio.put((generation, text if first else None, show, samples, rate))
                        first = False
                        self._in_flight = None  # the player prints it from here
            except Exception as e:
                print(f"   [speech failed: {e}]")
            if self._in_flight and show:  # synthesis failed, so at least show the line
                print(f"jarvis> {text}")
            self._in_flight = None
            await self._audio.put((generation, None, show, None, None))

    async def _player(self) -> None:
        while True:
            generation, text, show, samples, rate = await self._audio.get()
            if generation != self._speech_generation:  # cut off; _interrupt() already printed it
                continue
            if samples is None:  # end of a line
                self._lines -= 1
                if not self._lines:
                    self._follow_up = not self.standby
                continue
            await self._mic_free.wait()
            if generation != self._speech_generation:
                if text and show:
                    print(f"jarvis (not spoken)> {text}")
                continue
            if text and show:
                print(f"jarvis> {text}")
            sd.play(samples, rate)
            await asyncio.to_thread(sd.wait)
