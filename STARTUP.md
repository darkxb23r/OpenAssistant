# Starting Jarvis

Run every command from the project folder:

```
cd %USERPROFILE%\Documents\Projects\OpenAssistant
```

## 1. One-time setup

Repeat after a fresh install or a package update.

1. **Python packages**
   ```
   pip install -r requirements.txt
   pip uninstall -y onnxruntime
   pip install --force-reinstall --no-deps onnxruntime-gpu==1.31.1
   ```
   The last two lines restore the GPU speech engine, which the first install quietly replaces with a CPU-only one.
2. **Build the browser interface**, once, and again whenever `web/` changes:
   ```
   cd web
   npm install
   npm run build
   cd ..
   ```
3. **Logins:** Claude Code logged in with the Max account. Don't set `ANTHROPIC_API_KEY`; Jarvis refuses to start if it's set.
4. **Optional extras,** switched on by filling in `%USERPROFILE%\.jarvis\secrets.env`:

   | Feature | Settings |
   |---|---|
   | Texts and calls | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_NUMBER`, `MY_PHONE_NUMBER`, `JARVIS_CALL_PIN` |
   | ElevenLabs voice | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, optionally `ELEVENLABS_MODEL`. Kokoro is used without these |
   | Mail | `MAIL_USER`, `MAIL_PASSWORD` (plus `MAIL_HOST`, `DAV_URL`) |
   | Codex for coding | `JARVIS_CODEX=1`, plus `codex login`. Off by default, so without this the coder uses Claude |

## 2. Starting Jarvis: pick one

**A. Voice at this PC** (mic and speakers, "Hey Jarvis"):
```
python run_jarvis.py --voice
```

**B. The core, used from the browser** (the default):
```
python run_jarvis.py
```
1. In a second window, pair the browser once:
   ```
   python jarvisctl.py pair --name desktop
   ```
2. Open the address it prints. It's on `http://localhost:8790` and carries a one-time code. After that, just open `http://localhost:8790`.

**C. Typing in this window:**
```
python run_jarvis.py --text
```

Add `--new` to any of them to start a fresh conversation. With the phone set up, give it about a minute after startup before texting or calling Jarvis.

## 3. Controlling a running Jarvis

```
python jarvisctl.py status             what's running, who's connected, questions, tasks, orders
python jarvisctl.py attach             talk to it in this terminal
python jarvisctl.py standby on|off
python jarvisctl.py pause | resume     standing orders off / on
python jarvisctl.py stop | restart
python jarvisctl.py devices            list paired browsers and satellites (revoke ID / reset)
```

## 4. Start automatically at log-on (optional)

```
python jarvisctl.py install-service
```

This registers a Windows Task Scheduler task that starts the core when you log on. `uninstall-service` removes it.

## 5. A voice satellite on another machine (optional)

1. **On this PC,** make the bundle, then copy that folder to the other machine:
   ```
   python jarvisctl.py satellite-bundle C:\temp\jarvis-satellite
   ```
2. **On the other machine,** install its packages:
   ```
   pip install -r requirements.txt
   ```
3. **Give it a way to reach this PC's port 8790.** Simplest:
   ```
   ssh -N -L 8790:127.0.0.1:8790 you@this-pc
   ```
4. **On this PC,** make a pairing code:
   ```
   python jarvisctl.py pair --name kitchen --kind satellite
   ```
5. **On the other machine,** start it with that code. Afterwards it's just `python satellite.py`.
   ```
   python satellite.py --pair CODE
   ```

## 6. Standing orders (optional)

1. Copy `charter.example.toml` to `%USERPROFILE%\.jarvis\charter.toml`.
2. Edit it to set which orders run (backup check, machine watch, mail triage, daily brief), when, and the quiet hours.
3. Jarvis asks once to adopt it, at the PC or in the browser. Any later change asks again.
4. `python jarvisctl.py pause` stops them at any time.

## 7. Permissions: fewer questions (optional)

Your own rules live in `%USERPROFILE%\.jarvis\permissions.toml` (`permissions.example.toml` explains every
setting). Jarvis rereads it whenever it changes; no restart needed. Edit it by hand, or:

```
python jarvisctl.py rules                         list them, numbered
python jarvisctl.py allow "git *"                 never ask before these commands
python jarvisctl.py always-ask "git push *"       always ask before these
python jarvisctl.py trust-folder C:\path\to\project   work there freely, like the free zone
python jarvisctl.py ask-folder %USERPROFILE%\Documents\Finance   always ask, even to read
python jarvisctl.py use-claude-settings C:\path\to\project   follow that project's .claude settings
python jarvisctl.py remove 3                      (a number from `rules`, or the exact value)
```

For a working session, say "stop asking" (two hours) or "stop asking for 30 minutes"; "start asking
again" ends it. Other wordings ("you're authorized to run anything") don't switch it on.

These work whether or not Jarvis is running. Or just say it: "stop asking about git commands" or "always
ask before you push". Jarvis proposes the rule and you confirm it at the PC or in the browser (never by text,
call or satellite). Some things still always ask: power and disk commands, secret folders like `.ssh`,
Jarvis's own folder, and machines it hasn't used before.

## Things to know

- `--web` and `--headless` no longer exist. Unknown flags are refused.
- Logs are in `%USERPROFILE%\.jarvis\logs\jarvis.log`.
- Run `jarvisctl.py` normally, not with Python's `-I` flag (it needs the project folder on its path).
- More detail: `CLAUDE.md` (how it all works) and `HANDOFF.md` (what's done, and the checks still to do by hand).
