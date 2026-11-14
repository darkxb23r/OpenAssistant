# OpenAssistant

An always-on personal assistant for your own PC or homelab, on Windows or Linux. You talk to it by voice or
text, from a holographic face in the browser, a voice satellite in another room, a terminal, or your phone. It
works through things with Claude, inside a permission layer that decides what it may do without asking.

Out of the box the assistant is called **Jarvis** ("Just A Rather Very Intelligent System"); give it any name you
like in the Settings panel.

> OpenAssistant is an independent hobby project. It is not affiliated with or endorsed by Marvel, Disney,
> Anthropic or any other company whose names or products it mentions.

## Features

- **One assistant, many ways in.** An always-on core holds the conversation; every client joins it:
  - the browser face (voice with a wake word, or typing; images, video and panels on screen; hand gestures)
  - voice satellites on other machines
  - terminals (`jarvisctl attach`)
  - texts and calls through Twilio
- **Permissions you control.**
  - Read-only work runs on its own; anything risky asks first, with yes / no / always.
  - It has a free zone (its workspace) for its own files and installs.
  - Your own rules live in `permissions.toml`, and a timed "trust mode" stops the asking for a while.
- **Long tasks and standing orders.**
  - Multi-step tasks run in the background.
  - A charter you write gives it standing orders: daily brief, machine watch, mail triage, backup check. It acts,
    then reports.
- **Its own browser.**
  - It drives a sandboxed browser (Playwright) you can watch live on the face.
  - The browser has its own profile and can't reach your local network.
- **Knowledge.** It keeps what it learns as an Obsidian vault in its workspace, kept tidy by a librarian helper.
- **Voices.** ElevenLabs when you have a key, otherwise the local Kokoro voice; Whisper for speech recognition.
- **Mail and calendar.** Optional, through IMAP/SMTP and CalDAV.
- **Coding.** A coder helper, optionally backed by OpenAI's Codex.
- **Self-updates.** It can change its own code in a copy, show you the diff, and switch only on your yes.

## Requirements

- **Python 3.11 or newer.**
- **Node.js 20 or newer**, to build the browser interface and for the assistant's own browser.
- **Claude Code**, installed and logged in with a Claude subscription. OpenAssistant runs Claude through Claude Code
  (the `claude-agent-sdk`). Don't set `ANTHROPIC_API_KEY`: it would bill per token, so OpenAssistant refuses to
  start with it set.
- Optional: an NVIDIA GPU (faster speech), Twilio (phone), an ElevenLabs key (voice), a mail server (mail and
  calendar), the `codex` CLI (Codex).
- Linux, for voice at the PC itself (`--voice`): PortAudio (`sudo apt install libportaudio2`, `dnf install
  portaudio` or `pacman -S portaudio`).

## Install

The install scripts do steps 2 to 4 below in one go, into `.venv`, and are safe to run again after an
update: `./install.sh` on Linux, `powershell -ExecutionPolicy Bypass -File install.ps1` on Windows (add
`--service` / `-Service` to start at boot or log-on). They still need Python, Node.js and Claude Code
installed, and say what's missing.

### 1. Get the code

```
git clone https://github.com/<you>/OpenAssistant.git
cd OpenAssistant
```

### 2. Python environment

Linux (use Python 3.11 or newer by name, e.g. `python3.12`, if your `python3` is older; check with
`python3 --version`):
```
python3.12 -m venv .venv
. .venv/bin/activate
python --version
pip install -r requirements.txt
```

Windows (PowerShell):
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

With an NVIDIA GPU, restore the GPU speech engine afterwards (the install above replaces it with a CPU-only one):
```
pip uninstall -y onnxruntime
pip install --force-reinstall --no-deps onnxruntime-gpu==1.31.1
```

The speech models (about 340 MB) download on first use into `models/`.

### 3. Build the browser interface

```
cd web
npm install
npm run build
cd ..
```
Run it again whenever `web/` changes.

### 4. Log in to Claude Code

Install Claude Code and log in once with `claude`. OpenAssistant uses that login.

## Run

```
python run_jarvis.py
```

This starts the core under a supervisor that restarts it after a crash or a self-update. Open
**http://localhost:8790**, press the button, and say "Hey Jarvis" (or press Space, or type).

Other ways to run it:

| Command | What it does |
|---|---|
| `python run_jarvis.py --voice` | Voice at this PC's own mic and speakers |
| `python run_jarvis.py --text` | Typing in the terminal |
| `python run_jarvis.py --new` | Start a fresh conversation (combines with the others) |
| `python jarvisctl.py status` | Control a running core: `status`, `pause`, `resume`, `standby`, `stop`, `restart`, ... |
| `python jarvisctl.py attach` | Talk to the running core from a terminal |

### Pair your browser

Until a browser is paired, any page of the interface on this PC can use it. Pair yours once:
```
python jarvisctl.py pair --name desktop
```
Open the address it prints. After that, only paired browsers can connect.

### From another computer

The core only listens on `127.0.0.1`. Reach it from a laptop through SSH:
```
ssh -N -L 8790:127.0.0.1:8790 you@your-pc
```
Then open http://localhost:8790 on the laptop and pair it the same way.

### As a service

```
python jarvisctl.py install-service
```
This installs a systemd user unit on Linux, or a log-on task on Windows. On Linux, also run `sudo loginctl
enable-linger $USER` once, so it keeps running when you're logged out.

## Settings

Settings and secrets live in `~/.jarvis/secrets.env` (on Windows `%USERPROFILE%\.jarvis\secrets.env`), outside
the project, so they never end up in git. Edit them in the interface's **Settings** panel. Secrets there are
input only: you can replace or clear them, never read them back. Changes apply when the assistant restarts.

| Feature | Settings |
|---|---|
| Name | `JARVIS_NAME`, `JARVIS_NAME_MEANING` (default: Jarvis) |
| ElevenLabs voice | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` (without these: the local Kokoro voice) |
| Mail and calendar | `MAIL_HOST`, `MAIL_USER`, `MAIL_PASSWORD`, `DAV_URL` |
| Phone (Twilio) | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_NUMBER`, `MY_PHONE_NUMBER`, `JARVIS_CALL_PIN` |
| Codex | `JARVIS_CODEX=1`, plus `codex login` |

Environment-only settings: `JARVIS_WORKSPACE` (the assistant's working folder, default `~/jarvis-workspace`) and
`JARVIS_WEB_PORT` (default 8790).

Optional files in `~/.jarvis`, with documented templates in this repo:
- `permissions.toml`: your own permission rules. See `permissions.example.toml`.
- `charter.toml`: standing orders. See `charter.example.toml`.

`STARTUP.md` has a step-by-step start guide. `CLAUDE.md` describes the architecture in depth, for working on the
code.

## Tests

```
python -I tests/run.py          # every test in its own process
cd web && npm test              # the interface's pure rules
```
The tests never call Claude, use only localhost, and never touch your real `~/.jarvis`.

## Credits

OpenAssistant began as a merge of two projects:
- a Python assistant built on `claude-agent-sdk` (the core, permissions, tasks, phone and voice)
- **[adewaskar/jarvis](https://github.com/adewaskar/jarvis)** by Aditya Dewaskar, MIT licensed

From adewaskar/jarvis come the browser interface in `web/` (the holographic face, the HUD, the blades and the
hand gestures) and the ideas behind `netguard.py` and `ui_tools.py`. Its license is kept in `web/LICENSE`; see
[`NOTICE.md`](NOTICE.md) for exactly what came from it. Thank you.

## License

MIT; see [`LICENSE`](LICENSE). The parts from adewaskar/jarvis remain under their own MIT license (`web/LICENSE`).
