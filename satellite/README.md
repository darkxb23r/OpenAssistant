# Jarvis voice satellite

A microphone and a speaker for Jarvis in another room or on another machine. It talks to a running
core (`python run_jarvis.py --core`) over the client protocol, as a paired device with no screen.

- "Hey Jarvis" and the end of your sentence are detected here (openWakeWord, Silero VAD).
- What you said is transcribed by the core (`/stt`) and Jarvis's replies are spoken in its voice by
  the core (`/tts`, Kokoro or ElevenLabs), then played here. No speech models run on the satellite.
- Permission questions are asked aloud; answer "yes", "no" or "always". Adopting the standing
  orders' charter can't be answered from a satellite: it tells you once that it's waiting at the PC
  or in the browser.
- Task and standing-order news is spoken when this is where you last talked to Jarvis.

## Requirements

- Python 3.11 or newer, a microphone and speakers.
- This folder plus `voice.py` and `protocol.py` from the Jarvis folder, in the same layout
  (`satellite/satellite.py` imports them from its parent folder). On the core,
  `python jarvisctl.py satellite-bundle DIR` copies exactly those files into a new or empty `DIR`;
  copy that folder to the satellite machine. After updating Jarvis, make a new bundle.
- `pip install -r satellite/requirements.txt` (Linux also: `sudo apt install libportaudio2`).
  openWakeWord downloads its small `hey_jarvis` model on first run.

## Reaching the core

The core listens on 127.0.0.1 only. On the core's own PC nothing is needed. From another machine,
tunnel to it first and leave the tunnel running:

```
ssh -N -L 8790:127.0.0.1:8790 user@core-pc
```

## Pairing (once)

On the core: `python jarvisctl.py pair --kind satellite --name kitchen` prints a one-time code (valid 10 minutes).
On the satellite:

```
python satellite/satellite.py --pair 123456
```

The device token is kept in your config folder, never in the project: `%APPDATA%\jarvis-satellite\token`
on Windows, `~/.config/jarvis-satellite/token` elsewhere (`--token-file` moves it).

## Running

```
python satellite/satellite.py              # --url ws://127.0.0.1:8790/ by default, --name for its own name
```

If the core stops or restarts, the satellite redials (1 s, 2 s, 4 s ... at most 30 s). It stops,
saying why, when the core says it was revoked (`jarvisctl devices revoke ID`), isn't paired, or
speaks another protocol version: pair it again, or update it.
