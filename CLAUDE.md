# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Jarvis: a voice-driven personal assistant for a homelab PC running Windows or Linux. It's a git repo (branch `main`).
Python 3.11 or newer (`jarvis.py` and `run_jarvis.py` exit on anything older). The OS-specific parts are the shell
tools (`SHELL_TOOLS`: Bash, plus PowerShell on Windows), the `sysadmin` subagent (`sysadmin_agent()`), the install
advice in `SYSTEM_PROMPT` (`INSTALL_HINT`), the guardrails' command lists (both OSes' commands are always checked,
since a Windows PC can run Git Bash and Linux can run `pwsh`), and the voice layer's CUDA loading.

- `jarvis.py`: the orchestrator, built on `claude-agent-sdk`. Claude, via the user's Claude Max login rather than an API key, coordinates; subagents do the work. It holds the prompt, agents, tools, MCP servers and conversation loop.
- `guardrails.py`: the whole permission layer, meaning everything that decides what Jarvis may do without asking. `jarvis.py` imports from it and sets `guardrails._io` at startup.
- `permissions.py`: the user's permission rules file (`~/.jarvis/permissions.toml`): parsing, pattern matching and writing, stdlib only (jarvisctl uses it). In `GUARDED_FILES`.
- `updater.py` and `run_jarvis.py`: self-updates and the restart/rollback supervisor (see below).
- `control.py`, `jarvisctl.py` and `terminal.py`: controlling a running core from this PC or over SSH, and
  installing it as a service (see "The core as a service" below).
- `tasks.py`: long tasks, meaning task files plus a background runner (see below).
- `agency.py`: standing orders, work Jarvis does on its own from the user's charter (see "Standing orders" below);
  `watchers.py` holds each kind's quick check.
- `phone.py`: Jarvis texts and calls the user through Twilio, and the user can text or call Jarvis (see below).
- `mcp_launch.py`: starts the outside MCP servers with their secrets.
- `voice.py`: the voice I/O layer (wake word, VAD, STT, TTS), imported only in `--voice` mode and by the satellite.
- `satellite/satellite.py`: a voice satellite, a mic and speaker for Jarvis on another machine (see "Voice satellite" below).
- `elevenlabs.py`: Jarvis's voice from ElevenLabs when `secrets.env` has `ELEVENLABS_API_KEY` (see below).
- `web.py`, `ui_tools.py`, `netguard.py` and `web/`: the browser interface (`--core`, see below). `web/` is the
  React/Three.js front end from adewaskar/jarvis (MIT, `web/LICENSE`), without its Node bridge or its direct
  (API key in the browser) mode.
- `protocol.py` and `devices.py`: how a client introduces itself (hello/welcome), and paired devices with their
  tokens, pairing codes and media tickets (see "Clients, pairing and tickets" below).
- `tests/`: one script per area, plus `run.py`.

## Commands

Linux: use `python3`, and a virtual environment (`python3 -m venv .venv && . .venv/bin/activate`; newer Debian and
Ubuntu refuse system-wide pip installs). Voice mode also needs PortAudio from the system (`sudo apt install
libportaudio2`, `dnf install portaudio` or `pacman -S portaudio`); `--text` and `--core` don't. The
phone needs `cloudflared` on PATH. On Windows it can also live in `%LOCALAPPDATA%\cloudflared`.

```
pip install -r requirements.txt
# kokoro-onnx/openwakeword/faster-whisper also install CPU "onnxruntime", which shares files with
# onnxruntime-gpu. After any install, remove it and restore the GPU build:
pip uninstall -y onnxruntime
pip install --force-reinstall --no-deps onnxruntime-gpu==1.31.1
python run_jarvis.py          # the core (same as --core), under the supervisor (restarts after self-updates):
                              # no mic, speakers or keyboard; reached through the browser interface
                              # (http://localhost:8790, build it first, below), satellites, the phone, jarvisctl attach
python run_jarvis.py --voice  # voice mode at this PC's own mic and speakers
python run_jarvis.py --text   # keyboard mode, no voice deps loaded
python run_jarvis.py --new    # start a fresh conversation (combinable with the others); unknown flags are refused
python jarvisctl.py status    # control a running core (every command below)
cd web && npm install && npm run build    # once, and after changing web/; `npm run dev` for live editing (:5173)
cd web && npm test                        # the face's pure rules (lib/hold.ts), with Node's own test runner
python jarvis.py ...          # same flags without the supervisor (an applied update just exits)
python tests/run.py           # all tests, each in its own process; non-zero exit on failure
python tests/run.py zones     # only tests whose file name contains "zones"
python satellite/satellite.py --pair CODE   # a voice satellite (own requirements: satellite/requirements.txt)
python jarvisctl.py satellite-bundle DIR    # just the files a satellite on another machine needs
```

- Each test sets up a throwaway home folder and workspace by setting `USERPROFILE`/`HOME`/`JARVIS_WORKSPACE` before importing, so the tests never touch the real `~/.jarvis` or `~/.ssh`. Tests patch guardrail state through the `guardrails` module (e.g. `g.judged_safe`, `g._io`), not `jarvis`.
- `test_updater.py` uses a throwaway git repo and points `updater.PROJECT_DIR` and `run_jarvis.PROJECT_DIR` at it. `test_mail_launch.py` is skipped if the mail server's venv isn't installed.

- Startup exits if `ANTHROPIC_API_KEY` is set (it would override the Max login and bill per token).
- `JARVIS_CONTAINED=1` treats the whole machine as Jarvis's free zone. Set it only inside the VM.
- `JARVIS_WORKSPACE` sets the agent's working folder (default `~/jarvis-workspace`). Codex can only write there.
- Codex is optional and off by default. `JARVIS_CODEX=1` (environment, or `~/.jarvis/secrets.env`; the environment
  wins) turns it on; it then needs the `codex` CLI on PATH, logged in via `codex login` (ChatGPT Plus), or startup exits.
- `JARVIS_NAME` and `JARVIS_NAME_MEANING` (environment, or `~/.jarvis/secrets.env`; the environment wins) rename the
  assistant (`identity.py`); unset, it's Jarvis, "Just A Rather Very Intelligent System". The name goes into
  `SYSTEM_PROMPT` (`{name}`, `{name_meaning}`), the phrases it answers to (`_LEAD`/`_TAIL`, so standby, trust and the
  browser), Whisper's hint words, the phone's greeting, and the interface via the welcome frame's optional
  `state.assistant` (title, HUD brand with the meaning under it, the log, the hints, and the face's wake word,
  `setCallName()` in `voice.ts`). "Jarvis" is still answered to. The PC's `--voice` mode and satellites keep
  openWakeWord's "Hey Jarvis" (no model for another name). A name is 1–32 letters, digits, spaces, `. ' -`;
  anything else falls back to Jarvis. It's in `~/.jarvis`, so the model can't rename itself.
- **Settings panel** (`settings.py`, guarded; the face's SETTINGS button, `ui/Settings.tsx`): edits `secrets.env` from
  the interface over the socket (`settings_get`, `settings_set`, `settings_restart`). Only `settings.FIELDS` can be
  written, each checked (one line, no control characters, a shape per key), so nothing else can be slipped into the
  file; `JARVIS_CONTAINED`, `JARVIS_WORKSPACE` and the port aren't there. Secrets are input only: `listing()` sends
  them as set or not, never a value or any part of it; they can be replaced or cleared. The file is rewritten
  atomically with its other lines and comments, mode 0600; the log names the keys changed, never the values. Only a
  face with trust `physical` or `device` may (like a hard question's answer); not satellites, terminals or phones. In
  open mode any page of the interface on this PC counts as a device, so pair the browser (`jarvisctl pair`) to keep
  it to yours. Changes apply at the next start; the panel offers RESTART JARVIS (the control `restart` command).
- `JARVIS_WEB_PORT` moves the browser interface off 8790. Never 8787: that's the Caveman proxy.

## Architecture

One always-on **core** (`run_jarvis.py` → `jarvis.py --core`, the default) owns the conversation with Claude, the
permission layer, tasks and standing orders. Nothing is spoken or typed at the PC; people reach it through
**clients**, each a channel of one `hub.Hub`. The sections below have the details.

- **Hub** (`hub.py`): every channel is duck-typed (`say`, `ask`, `hear` or pushed lines, `notify`, ...); turns run one at
  a time (`run_turns()`); a line keeps the `source`, `trust` and `kind` it arrived with. Jarvis speaks on, and the
  interface tools draw on, the channel used last (`primary()`, `display_target()`); `hub.Scene` replays the look to a
  new screen. `--voice` (`voice.Voice`) and `--text` (`TextIO`) add a local channel at the PC instead.
- **Clients**, all on the interface server (`web.py`, 127.0.0.1 only; elsewhere through `ssh -L`), one `WebClient` per
  socket: the **face** (`web/`, a browser: display, gestures, `/stt` `/tts` speech), the **terminal** (`jarvisctl
  attach`, `terminal.py`: the control token, `trust="physical"`) and the **voice satellite** (`satellite/`: a mic and
  speaker, no display). The **phone** (`phone.py`, Twilio texts and calls) is a hub channel of its own.
- **Protocol v1** (`protocol.py`, frozen; a v2 would be served alongside it): `hello` → `welcome` or close 4001
  (revoked), 4002 (version, or no hello first), 4003 (pairing required); then the frames in `web.py`'s docstring.
- **Pairing and tickets** (`devices.py`): `jarvisctl pair --kind face|satellite` makes a one-time code; the device
  keeps a token (only its sha256 is stored). Once anything is paired, every client needs a token, and `/file /img
  /media /stt /tts` need a 12-hour media ticket, renewed with a `ticket` frame (the face) or a new hello (the satellite).
- **Who may do what**: a line's trust is `physical` (the PC, a terminal), `device` (a face or satellite), `sms` or
  `call` (a text answers a question only with its code). `guardrails.py` decides every tool call: read-only rules,
  free zones, "always" rules, trust mode, a Haiku safety check, else a question. Texts, calls and satellites (`jarvis.REDUCE_ONLY_SOURCES`) can only make Jarvis do
  less: never trust mode on or resuming the standing orders.
- **Work**: subagents for the main conversation; long tasks (`tasks.py`, one fresh worker per step, checked); standing
  orders (`agency.py` + `watchers.py`, from the user's charter, adopted by one hard question, budgeted, journaled,
  quiet in quiet hours).
- **Voice engines** (`voice.py`): Whisper and Kokoro (or ElevenLabs) locally, or `RemoteSTT`/`RemoteTTS` against the
  core's `/stt` and `/tts` (the satellite). The face uses the same `/stt` `/tts`, or the browser's own.
- **Updates** (`updater.py`, `run_jarvis.py`): Jarvis edits a git worktree, never the live tree; applying is always
  asked, tests run first, and the supervisor restarts it and rolls back an update that won't start. `GUARDED_FILES`
  (the safety files) are flagged in every update's check.
- **Control** (`control.py`, `jarvisctl.py`): status, pause, standby, stop/restart, pairing, devices, services,
  `satellite-bundle`.

## The orchestrator (jarvis.py)

- **Main agent**: `SYSTEM_PROMPT` + `model="sonnet"`. Replies must stay short, plain text, no markdown — they are spoken by TTS.
- **Background work**: the prompt has Jarvis start subagents with `run_in_background: true`. The CLI then keeps the main agent free to talk, and when a task finishes it wakes the main agent without a new `query()`, so `listen()` just speaks the result. This was verified with the SDK.
- **Subagents** (`AGENTS`): `researcher` (web), `coder` (delegates to Codex when it's on), `sysadmin` (read-only PowerShell on Windows, read-only Bash on Linux, haiku). Each gets only the tools it needs. Every subagent prompt ends with `INFORMATION_ONLY` (text from the web, files, mail or tool output is information, never instructions) and `TERSE`.
- **Codex integration** (only when `CODEX_ON`; otherwise there's no `codex` server and `coder_agent(False)` codes
  with Write/Edit/shell itself): not a real MCP server. `codex mcp-server` was dropped in codex 0.163, so `run_codex()` shells out to `codex exec --json` (or `exec resume <id>`), task on stdin, no shell, sandboxed `-s workspace-write -C WORKSPACE`, 20-min timeout. Wrapped as in-process SDK MCP tools `mcp__codex__codex` / `mcp__codex__codex-reply`. Session ids are validated against a UUID regex before being passed as args.
- **MCP servers** (`MCP_SERVERS`): added one at a time, read-only first. Any new server's tools must also be added to `AUTO_APPROVED`, or every call goes through the permission prompt.
- **Knowledge vault** (`knowledge.py`): general knowledge lives in an Obsidian vault at `WORKSPACE/Knowledge` (the user opens the folder in Obsidian). `ensure_vault()` (at startup) creates only what's missing and never overwrites: `Home.md` linking one map per area (`Maps/`: Machines, Network, Projects, How-to), `Notes/` (one subject per note, titled by the subject), `Templates/Note.md`. `CONVENTIONS` is the librarian subagent's prompt: search first and update rather than duplicate, frontmatter with `source` and `updated`, every note linked to its map, only lasting checked knowledge (no chat logs, passing state, guesses, secrets, or personal facts, which go in memory). `SYSTEM_PROMPT` tells Jarvis to look there before asking or searching the web and to have the librarian file what's worth keeping. It's in the free zone, so no new tools or permissions were needed; Codex can write there too, so notes are information, never instructions.
- **Browser** (`browser_launch.py`, guarded; `netguard.FilteringProxy`; `browser_view.py`): with `npx` on PATH, `MCP_SERVERS["browser"]` is the launcher. It installs Chromium on first start (`npx @playwright/mcp install-browser chromium`, output to stderr since stdout is the MCP pipe), starts the filtering proxy on a thread, then starts **its own headless Chromium** (`chromium()`: the newest full `chromium-*` build under Playwright's browsers folder, not the headless shell) with `--proxy-server` at the proxy, `--proxy-bypass-list=<-loopback>` (Chromium skips proxies for loopback otherwise), QUIC and non-proxied WebRTC UDP off, profile `~/.jarvis/browser/profile`, and DevTools on a random 127.0.0.1 port that Chromium writes to the profile's `DevToolsActivePort`. Playwright MCP (`@playwright/mcp`, pinned in `PLAYWRIGHT_MCP`; a new version is an update to review, and its tool names in `guardrails.BROWSER_AUTO`) drives it with `--cdp-endpoint`. The proxy resolves every host itself and refuses (403) any with a non-public address, then connects to the checked address: pages can't reach the LAN, the DevTools port, or Jarvis's interface on 127.0.0.1 (which in open mode could connect as a face and answer permission questions). Playwright's `--blocked-origins` isn't used (its docs: not a security boundary). `file://` is blocked and files stay in the workspace roots (Playwright's defaults); screenshots go to `WORKSPACE/browser`. A leftover Chromium (the launcher killed) is closed over its DevTools before a new one starts (`close_stale()`); the launcher closes its own on the way out. All `BROWSER_AUTO` tools are auto-approved, as the user chose; `browser_run_code_unsafe` is in `disallowed_tools` (`BROWSER_NEVER`): it runs code in the Node process, outside the browser. Main session only (not in `WORKER_SERVERS` or agency). **Live view**: `browser_view.LiveView` attaches to every tab over DevTools while someone watches and screencasts it (`Page.startScreencast`, JPEG, ≤ 8 fps; the tab that repainted last is shown); `web.py` serves it at `/browser/live` as MJPEG (`multipart/x-mixed-replace`, a media ticket like `/file`, 503 when the browser isn't running), and the face's `browser` blade (sticky, id `browser-live`) plays it in an `<img>`. It's view-only. "Show the browser" / "hide the browser" (`browser_request()`, said casually like standby) are handled in the main loop from any source, with no Claude turn: `browser_view.toggle()` puts the blade on the screen in use (`hub.display`) or sends `{"type": "blade_close", "id"}` (the scene forgets it too). Jarvis can do the same with the `browser_view` tool (`mcp__jarvis__browser_view`, auto-approved).
- **Memory**: two layers. `continue_conversation=True` resumes the last SDK session in `WORKSPACE` across restarts (`--new` skips it). Long-term facts live in `~/.jarvis/memory.md`, written by the in-process `remember`/`forget` tools and injected into `SYSTEM_PROMPT` (a `.format()` template) at startup. `~/.jarvis` must stay outside `WORKSPACE`, because Codex can write anywhere in the workspace; startup exits if it is inside.
- **Persona**: the "How you talk" part of `SYSTEM_PROMPT` sets a polished, dry-witted butler manner. It asks for dates only as precise as needed: relative within about a week ("this Wednesday", "next Friday at 7:05 PM"), otherwise "Friday the 13th". Names are full, times twelve-hour, and he calls the user "sir" whenever it suits (the phone's greetings do too).
- **Timestamps**: `main()` prefixes every query with `[spoken_now()]`, e.g. "Wednesday, November 11, 2026, 5:36 PM Mountain Standard Time", so Jarvis needs no lookup for "now" and mirrors the spoken format. The prompt also bans shell commands for time; other zones go through the `time` MCP tools.
- **Streaming speech**: `include_partial_messages=True`; `listen()` splits main-agent `text_delta` stream events into sentences and calls `say()` per sentence. `AssistantMessage` text is not spoken again. A text block that is just `QUIET` is never said: the prompt has Jarvis reply that when a background result isn't worth words (a step along the way, a stopped job, news already given). `listen()` holds a block's opening while it could still be `QUIET`.
- **Speaking style**: "How you talk" asks for about 25 words, the gist, no identifiers, no recap of how work was done or of the user's own instructions, and acting on routine next steps without asking (with before/after examples taken from real replies). Keep replies short there, not with a mechanical cap.
- **Mail and calendar** (mailcow at `mail.example.com`; HTTPS on port 8443 locally; IMAP 993 and SMTP 587):
  - **Account:** Jarvis has its own mailbox, `jarvis@example.com`, logged in with an app password limited to IMAP, SMTP and DAV. The user's SOGo calendar is shared to it read-only, and the prompt tells Jarvis both of these.
  - **Servers:** both are reviewed, pinned, and live outside this repo under `~/.jarvis/mcp/`:
    - `mcp-mailcow` 1.0.3 runs in its own venv, `~/.jarvis/mcp/mailcow`, because it needs `mcp<2` while `mcp-server-time` needs `>=2.3`. It runs in user mode.
    - `mcp-twake-dav` 4.2.1 is under `~/.jarvis/mcp/twake-dav`, installed with `--ignore-scripts`.
  - **Launching:** `mcp_launch.py mail|calendar` reads `~/.jarvis/secrets.env` (`MAIL_HOST`, `MAIL_USER`, `MAIL_PASSWORD`, `DAV_URL`). It passes each server only its own keys, strips the TLS-off variables, and keeps mailcow's plaintext audit log in `~/.jarvis/logs`.
    - Secrets never go into `MCP_SERVERS`: the SDK puts that config on a command line, which `Get-CimInstance` can list.
    - The servers are added only when `mcp_launch.configured()` is true.
  - **Permissions:**
    - `MAIL_AUTO` and `CALENDAR_AUTO` are auto-approved.
    - `CONFIRM_TOOLS` always ask, with a spoken summary: send, reply, forward, delete, `set_custom_flag`, event create/update/delete, `respond_to_invitation`, and `delete_contact`.
    - Alarms and contact create/update go through the normal question.
    - `DAV_APPROVAL=client` turns off twake's own elicitation prompt, because Jarvis's check is the gate.
    - When upgrading either server, re-list its tools and classify any new ones.
- **Self-updates** (`updater.py`, tools `mcp__updates__*`): the live project folder is `guardrails.PROJECT_DIR`. It's in `PROTECTED_DIRS`, and shell commands naming it match `PROJECT_MENTION`, so Jarvis never edits it directly. Instead:
  1. **`start_update`** (auto-approved) adds a git worktree on branch `update/<name>` under `WORKSPACE/updates/<name>`. The coder subagent and Codex edit it there; that folder is inside Codex's sandbox. The live tree must be clean and on `main`.
  2. **`check_update`** (auto-approved) commits the copy, prints the diff, and lists any changed `GUARDED_FILES` (the safety files). It gets a one-line SAFE/CONCERN verdict from a fresh tool-less Sonnet `review()`. It deliberately runs none of the new code.
  3. **`apply_update`** is in `CONFIRM_TOOLS`, so it always asks; `updater.describe()` writes the question. It refuses if the branch changed since the check or `main` moved on. It runs the copy's `tests/run.py`, writes `ROLLBACK_FILE`, fast-forwards `main`, removes the worktree, and sets `restart_requested`.
  4. **Restart:** `listen()` sets `_restart` after that reply. `main()` waits for speech to finish, then `os._exit(RESTART_EXIT_CODE)`, since a text-mode `input()` thread would otherwise keep the process alive.
  5. **Supervisor:** `run_jarvis.py` restarts Jarvis, dropping `--new`. If Jarvis exits with an error while `ROLLBACK_FILE` still exists, it rolls back:
     - it keeps the broken code on `failed-update/<name>-<time>`;
     - it runs `git reset --hard` to the saved commit;
     - it writes `NOTE_FILE`, which the old version speaks at startup (`updater.startup_note()`).

     A successful start deletes `ROLLBACK_FILE` instead.
  - `run_jarvis.py` is stdlib-only so a broken update can't break the rollback. Keep its constants in step with `updater.py`.
- **Long tasks** (`tasks.py`, tools `mcp__tasks__*`, all auto-approved):
  - **Task file:** `create_task` writes `WORKSPACE/tasks/<name>.md`. It holds status, goal, done-when, a checklist (`- [ ]`, `- [x]`, or `- [?]` for waiting on the user), the user's answers, and a log. Entries are single lines (`one_line()`), and `load()` and `render()` round-trip it.
  - **Runner:** `start_task` runs `_run()` as an asyncio task in Jarvis's own loop. For each unfinished step, `do_step()` starts a fresh worker session (`run_worker()`, using `jarvis.worker_options()`) that sees only the rendered task and its one step.
  - **Ending a step:** the worker must call `mcp__step__step_done` with a summary and evidence, or `step_blocked` with a question. A tool-less Haiku `verify()` checks the evidence. `ATTEMPTS_PER_STEP` is 2 (the second try gets the checker's note), and after that the step becomes `?` and the user is told. Usage-limit errors pause for `USAGE_LIMIT_PAUSE_S` without using up a try.
  - **Lifecycle:** `answer_task` records the answer and restarts the task; `pause_task` cancels it. `resume_all()` at startup restarts tasks whose status is still `running`.
  - **Messages:** `tasks.say`, which is `_io.say`, announces when a task finishes, needs the user, or fails. `guardrails.current_task`, a contextvar set in `_run()`, adds ", for the <name> task" to workers' permission questions.
  - **Workers:** they get `WORKER_SERVERS` (codex, machines, time, mail, calendar, plus caveman), the same `AGENTS`, `AUTO_APPROVED`, `ask_before_risky` and read hook, and `WORKER_PROMPT`. They don't get zones, updates or memory.
  - Tests stub `tasks.run_worker` and `tasks.verify`.
- **Caveman** (local proxy at `127.0.0.1:8787`, https://docs.caveman.so):
  - **Routing:** `detect_caveman()` runs once at startup. If the proxy answers and `~/.caveman/bin/caveman-mcp.exe` exists, then `caveman_env()` points `ANTHROPIC_BASE_URL` at `/w/jarvis` with `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1`, so the Max login passes through. `caveman_servers()` adds caveman-mcp, which compression needs.
  - **Workers:** they also load the installed Caveman plugin (`caveman_plugin()`, newest version under `~/.claude/plugins/cache/caveman/caveman`) with `CAVEMAN_DEFAULT_MODE=caveman`. Subagent prompts end with `TERSE`. Jarvis's own spoken replies keep their manner.
  - **Kept direct:** `judged_safe`, `updater.review` and `tasks.verify`, because compression is lossy.
  - **Secrets:** `~/.caveman` is in `SECRET_DIRS`, because `ccr.db` keeps exact originals.
  - **Failure mode:** if the proxy stops mid-session, requests fail until Jarvis restarts.
- **Models**: only `MODELS = ("opus", "sonnet", "haiku")`; never Fable, and `test_models_and_codex.py` checks that the word doesn't appear in the code.
  - Chat is `CHAT_MODEL` (Sonnet).
  - Subagents: `architect` is Opus (plans, hard problems); `researcher` and `coder` are Sonnet; `sysadmin` is Haiku.
  - Task steps run on Sonnet unless the step text starts `[opus]` or `[haiku]` (`tasks.Step.model`). `worker_options()` refuses any other model.
  - Checks: the safety check and `tasks.verify` use Haiku; the update review uses Sonnet.
- **Codex fallback**: if Codex's JSON errors or stderr match `CODEX_LIMIT` (usage limit, 429...), `run_codex` sets `_codex_paused_until` for `CODEX_PAUSE_S` (30 min) and returns `CODEX_UNAVAILABLE`. The `coder` subagent then codes itself with Write/Edit/shell, which still go through the guardrails.
- **Task grants** (part 3, `guardrails.py`):
  - **Declaring:** `create_task` takes `needs` (folders, machines, command prefixes). `grant_problems()` refuses interpreters (`EXACT_ONLY`), chained commands, deletes, installs, `ASK_OFF_VM` and always-asks, and protected folders.
  - **Approving:** `mcp__tasks__start_task` is in `CONFIRM_TOOLS`. `tasks.approval_summary()` builds the question and calls `offer_grants()`, which snapshots exactly what it shows. A yes runs `accept_grants()`, which writes `~/.jarvis/task-grants/<task>.json` and never reads it back from the task file.
  - **Enforcing:** inside that task's runner (`current_task`), `task_grants()` extends `in_free_zone()`, counts its machines as handed over in `location_check()`, and lets `task_allows()` pass granted prefixes, locally or as `ssh <granted machine> <prefix> ...`. That check comes after `location_check`, so grants never beat an always-ask.
  - **Ending:** `drop_grants()` runs when the task is done or fails.
  - The `order-` prefix (`tasks.ORDER_PREFIX`) is kept for standing orders: `create_task`, `start_task` (and the
    other task tools) and `tasks.start()` refuse it.
- The Caveman proxy is started at launch by `detect_caveman()` if it isn't listening (`caveman start`, fully detached). Jarvis never stops it, because the user's own Claude Code routes through it. Never run `caveman stop` or other state-changing subcommands to "inspect" them.
- **Phone** (`phone.py`, Twilio; on when `secrets.env` has `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_NUMBER`, `MY_PHONE_NUMBER`, and optionally `JARVIS_CALL_PIN`):
  - **How Twilio reaches Jarvis:**
    - `Phone.start()` runs a uvicorn and starlette server on `127.0.0.1:8765` (`_QuietServer` leaves Ctrl+C alone).
    - It starts a cloudflared quick tunnel from `%LOCALAPPDATA%\cloudflared\cloudflared.exe` (installed from GitHub with its checksum verified), or `cloudflared` on PATH (Linux).
    - It points the Twilio number's `SmsUrl` and `VoiceUrl` at the new tunnel address, which changes every start. A fresh address can take about a minute to resolve.
  - **Webhook checks:** every webhook is checked with `twilio_signature()`, which matches Twilio's own `RequestValidator`, rebuilt from `self.public`. Only `MY_PHONE_NUMBER` is served. Incoming calls also need `JARVIS_CALL_PIN` through `<Gather>`.
  - **Media streams:** TwiML `<Connect><Stream>` carries a one-time token parameter that the `/media` websocket must present; the websocket upgrade is signature-checked too. Outbound calls use `MachineDetection`, and voicemail hangs up and counts as missed.
  - **`CallSession`:** half-duplex. It converts 8 kHz μ-law to 16 kHz, runs the openwakeword VAD and Whisper, and puts ("call", text) on the inbox. Kokoro output is converted to 8 kHz μ-law and sent with a `mark`, and `speaking` lasts until Twilio confirms the mark. `call_models()` reuses the voice layer's models, or loads them for calls in text mode or the core.
  - **`Channels`:**
    - **Inputs:** `hear()` races the local input and the phone inbox. It keeps the losing task, because a cancelled `input()` thread would swallow a line. `source` is `local`, `sms` or `call`.
    - **Replies:** to a text they go back by text (`end_of_turn()`), on a call they're spoken on the call, and `notify()` always texts task news.
    - **Questions:** sent by text carry a 4-digit code (`SMS_ANSWER`, "YES 4821"), because caller ID can be faked.
      One a text or call can't answer (`phone_may_answer()`: the charter's `only`) gets no code and no call, only
      one plain text (`waiting_elsewhere()`); texted answers to it get `ANSWER_ELSEWHERE`.
    - **Calling:** `_watch()` texts and rings once per question unanswered after `ANSWER_AFTER_S` (60 s). A missed call calls `_park()`, which turns on quiet standby and texts. The next contact calls `returned()`, which asks again.
    - **On calls:** `GOODBYE` hangs up, and the main loop's quit words only count at the PC.
  - **Tools:** `text_me` and `call_me` (`mcp__phone__*`) are auto-approved, since they only reach the user's own number. `guardrails.answer_hooks` tells Channels when a question is answered.
  - **Calls at a set time:** `call_me_at` (`at` local `YYYY-MM-DD HH:MM` or `HH:MM`, within a week; `about`) and
    `scheduled_calls` (list, or cancel `all`/`HH:MM`), both auto-approved, kept in `~/.jarvis/scheduled-calls.json`
    so restarts keep them. `Channels._watch()` takes due ones off (`due_calls()`) and calls `phone.on_call_due`,
    which `main()` sets to `hub.own_turn(CALL_DUE...)`: a turn with no channel (nothing said) in which Jarvis
    checks where things stand and rings with the outcome as `call_me`'s reason, or texts if the call fails.
- **Speaking to an empty room:** a turn Claude starts itself (`hub.begin_injected()`) goes to `primary()` only if
  `someone_here()` (any channel used within `HERE_S`, 15 min); otherwise its words go to `hub.unheard`, and
  `prompt_for()` tells Claude at the next turn what nobody heard.
- **Logs**: `~/.jarvis/logs/jarvis.log` rotates at midnight and keeps 30 days. It sits in a secret folder, so Jarvis can't read it; it holds mail text and command output.
  - `jarvis.py`'s `setup_logging()` attaches the handler to the `jarvis` logger.
  - `jarvis`: user lines, Jarvis and subagent text, tool calls and results (cut to `guardrails.short()`), background tasks, and per-turn timing.
  - `jarvis.permissions`: every `ask_before_risky` outcome plus its reason (auto-approvals, questions, answers, trust mode). `ask_before_risky` wraps `decide()`.
  - `jarvis.updates`: update steps.
  - The supervisor appends rollback lines to the same file, plain-text, while Jarvis isn't running.
- **Isolation**: `build_options()` sets `setting_sources=[]` and `ENABLE_CLAUDEAI_MCP_SERVERS=false` so Jarvis does not inherit the user's Claude Code settings, plugins, hooks, CLAUDE.md, or claude.ai connectors. Keep it that way.

### Permission model

`can_use_tool=ask_before_risky` handles anything not in `AUTO_APPROVED` (tools in that list bypass the callback entirely; the SDK's `CanUseToolShadowedWarning` at startup is expected). Auto-allowed:

- Bash/PowerShell commands that pass `is_read_only()`: command starts with a whitelisted head (`READ_ONLY_BASH` / `READ_ONLY_POWERSHELL`, whole-word match, so every argument the head accepts must be harmless) and contains no risky characters or `--output` (`BASH_RISKY`, `POWERSHELL_RISKY`). Bash heads whose options change things or never return are refused with those options (`READ_ONLY_BAD_ARGS`, `bash_args_ok()`): `journalctl --vacuum-*`/`--rotate`/`-f`, `tail -f`, `ss -K`, `date -s`, `hostname NAME`, `docker logs -f`. PowerShell may pipe only into `POWERSHELL_PIPE_OK` cmdlets; `Where-Object` is safe only because braces are banned. `-wait` is rejected.
- `FILE_TOOLS` (Write/Edit/NotebookEdit) when the resolved path is inside `WORKSPACE`.
- An exact "always" rule from `~/.jarvis/approved.txt`.
- Then `location_check()` decides by where the command acts:
  - **Free zone:** `WORKSPACE` plus the absolute paths in `~/.jarvis/free-zones.txt`, or the whole machine when `JARVIS_CONTAINED=1` (the planned VM).
  - **Protected:** `PROTECTED_DIRS` (the `SECRET_DIRS`, the project, and the OS dirs: Windows' `SystemRoot`/`ProgramFiles`; on Linux `/boot`, `/etc`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/opt`, `/var`, `/root`, `/sys`, `/proc`, `/dev`, `/snap`) and anything in `ASK_ALWAYS` (power, disk/boot, encoded/downloaded code) plus `touches_secret()`, which reads each `.jarvis`/`.claude`/`.codex`/`.ssh` mention as part of its path. Absolute paths are checked as written and relative ones against a leading `cd`. A project's own `.claude` folder is fine; a relative mention with no leading `cd`, or one built from an unknown variable, asks. `native()` maps Git Bash `/c/...` paths to `C:/...` for every path check. Protected always asks, even for reads, since `ASK_ALWAYS` is checked before the read-only list.
  - **Other machines:** `remote_hosts()` parses UNC paths (`UNC_HOST`: only at the start of a path, after start/space/quote/`=`/`(`, so Git Bash's doubled backslashes inside strings like `"..\\source"` or `'\\n'` are never machines), `-ComputerName`, `docker -H`, `ssh`/`scp`/`rsync`. A new host asks; "always" then saves `host: <name>`, handing that machine over. Remote-execution commands whose host can't be parsed always ask.
  - **Off a VM:** `ASK_OFF_VM` asks: registry, services/tasks, security/firewall/users, `sudo`; on Linux also `su`/`pkexec`/`doas`, user and group tools, `ufw`/`iptables`/`nft`/`firewall-cmd`, `systemctl enable|disable|mask|...|daemon-reload`, `crontab` (except `-l`), `mount`/`umount`, `chown`/`chgrp`/`setfacl`/`chattr`, kernel modules, `sysctl -w`. `ASK_ALWAYS` adds Linux power/session (`systemctl suspend`...), partitioning and boot (`fdisk`, `parted`, `wipefs`, `update-grub`...), and hidden or downloaded code (`eval`, `base64 -d`, `curl ... | sh`).
  - **Installs and uninstalls (`INSTALLS`, including apt/dnf/pacman/zypper/apk/snap/flatpak/brew/dpkg/rpm/pipx installs and removals, but not their queries):** off a VM these ask unless `installs_into_free_zone()` holds, in which case the normal checks decide. That needs a single unchained command that is either:
    - run by a program inside the free zone (a venv's pip), or
    - one of the `INSTALL_FLAGS` tools (pip, npm, cargo, winget) with its location option pointing into the zone, or
    - scoop with `SCOOP_DIR` in the zone.
  - **Work inside an approved folder (`works_in_free_zone()`):** like accepting edits there, but for commands: "free", with no Haiku check, when the command starts with a single `cd`/`Set-Location`/`pushd` into the free zone (`CD_ANYWHERE` finds no other folder change), uses no variables, and every full path (`FULL_PATH`: drive, `C:` alone, `/c/` and other POSIX paths, `~`, `\\share`) and `..` path (`PARENT_PATH`) resolves inside it. Short `/b`, `//c` words count as Windows switches (`SWITCH`) except in a command that deletes or moves (`CHANGES`), where `/c` may be the drive. The floor (always-ask, secret, project, host checks) runs before it. Off a VM, a local `npm`/`pnpm`/`yarn install` (no `-g`) inside such a folder is allowed. Scripts it runs can still reach beyond the folder; that's the accepted tradeoff on a real PC. The prompts tell Jarvis and its workers to start shell work with `cd <folder full path> &&`.
  - **Deletes (`DELETES`):** allowed outright ("free") only if `deletes_only_free_zone()` sees nothing but flags and absolute free-zone paths, or (`chained_deletes_only_free_zone()`) exactly one such delete is chained only with read-only commands (`rm <ws>/x && ls <ws>`). Relative paths, variables, lists, other commands, two deletes, and a zone's own root all ask.
  - "Always" on anything that asked here saves only the exact command.
- Otherwise, in order: an "always" pattern (`<tool>: <program> [<subcommand>] *`, never for chained commands or `EXACT_ONLY` interpreters and runners like `python`/`ssh`/`powershell`/`perl`/`env`/`xargs`), trust mode, then `judged_safe()`. That is a fresh tool-less Haiku `query()` per unseen command, told the free zone, the handed-over hosts, and whether it's in a VM, and that the user wants ordinary work done without asking: `JUDGE_PROMPT` lists everyday project work (heredoc writes, the project's scripts and dev servers, killing their processes, localhost requests, WSL home folders, Docker Desktop restarts) as SAFE, and RISKY means plausible harm that's hard to undo.
- Before that: `remote_read_only()` allows one `ssh <handed-over host> "<commands>"` (only `BatchMode`/`ConnectTimeout`/`ServerAliveInterval`/`StrictHostKeyChecking` options, never `ProxyCommand`) whose every remote command passes `is_read_only("Bash")`, with no `$(`, `${` or backticks (they'd run locally). Haiku flipped on exactly these.
- `Monitor` (Claude Code's background-watch tool) is decided as `Bash` with its `command`. It replies SAFE or RISKY and is cached per command; failure or timeout means ask. Its `cwd` is `~/.jarvis` so its sessions never become the one `continue_conversation` resumes.
- **The user's rules (`~/.jarvis/permissions.toml`; `permissions.py` reads, matches and writes it, stdlib only; `permissions.example.toml` documents it):** `refresh_rules()` rereads it when its mtime/size changes, at every `decide()` and in both guard hooks. A broken file is logged and announced once (`rules_notice`, via `_io.notify`), keeping the last good rules; unknown keys count as broken. Where they apply in `decide()`:
  1. `commands.always_ask` (matched anywhere, every chained segment and inside quotes, `permissions.asking_rule`) and `folders.always_ask` (`ASK_FOLDERS`: file tools, `Read`/`Glob`/`Grep` via `guard_reads` (a search over a folder holding one counts), and shell commands naming a path inside in any slash style, from a leading `cd`, or via `..`: `command_ask_folder`) come first, read-only commands included. Each is a hard question and "always" saves nothing (`saves=False`). `guard_shell` routes them in too.
  2. The safety floor (`floor_check()`: `always_asks`, the project folder, unclear remoting, new machines), then exact approvals and the read-only list.
  3. `commands.allow` (`rule_allows`, `permissions.allowing_rules`): globs (`*` = anything, a trailing ` *` also matches the bare command, whitespace normalised, case-insensitive for PowerShell). In a chain (`; && || | &` or newlines, split ignoring quotes, which only splits more) every segment must match, be `is_read_only`, or be a bare `cd`; harmless redirects (`2>&1`, `>/dev/null`) are dropped first, and anything with `$(`, backticks, `<`/`>` (or PowerShell's `( ) { }`) is never covered. Patterns must start with a literal program name. Not inside a standing order (`attended`). A pattern is as broad as written (`git *` covers `git -c alias...`).
  4. `folders.claude_settings` (`CLAUDE_SETTINGS_FOLDERS`, validated like trusted folders): for a command that
     starts with one `cd` into such a project (`settings_project()`), `permissions.claude_rules()` reads its
     `.claude/settings.json` and `settings.local.json` live (Bash/PowerShell entries only; `X:*` becomes `X *`,
     bare = all). `project_asks()` (ask and deny rules) is a hard question next to `rule_asks`; `project_allows()`
     allows only where `location_check` said "check" (so the floor, `ASK_OFF_VM`, deletes outside the zone and new
     hosts still ask), before the judge, not in standing orders. Changing those files asks hard (`is_settings_file()`
     for file tools, `touches_settings()` for commands that aren't only reads), so Jarvis can't grant himself more.
  5. `folders.trusted` (`TRUSTED_FOLDERS`) join the zones in `in_free_zone()`, so they act like free-zone folders everywhere (edits, `works_in_free_zone`, deletes, the judge's prompt); `is_protected` still wins inside. `rule_problem()` refuses a protected folder, a drive root, the home folder or one holding it, and one holding `PROJECT_DIR`, with a notice. Not done with `permission_mode="acceptEdits"`, which approves edits before `can_use_tool` runs (so protected and always-ask folders would go unchecked).
  Every rule decision is logged: `auto-approved by your rule 'git *'`, `asking because of your rule '...'`.
- `mcp__rules__propose_rule` (`PROPOSE_RULE`, kinds `allow`/`always_ask`/`trust_folder`/`ask_folder`): Jarvis calls it when the user says "stop asking about X" / "always ask before Y" (the prompt says never on its own initiative). Validated first (`rule_problem`), then a hard question every time with `only={"physical","device"}` and `not_kinds={"satellite"}` (like the charter), so trust mode, "always", texts, calls and satellites never answer it; `approval_key` is `None`. On yes the tool writes the rule (`permissions.add`) and forces a reload. Main conversation only (not in `WORKER_SERVERS`).
- `mcp__zones__add_free_zone` (`ADD_FREE_ZONE`) always asks; "always" and trust mode never skip it. `zone_problem()` rejects relative, protected, or already-free paths without asking. On approval the tool creates the folder and appends it to `free-zones.txt`. It also adds it to `FREE_ZONES` live and clears the safety-check cache. `list_free_zones` (optional `under` root) and `remove_free_zone` are auto-approved, since they only ever take power away. Removing a folder edits the file in place and never removes the workspace.
- **Shell commands Claude Code approves itself:** its own read-only check runs commands like `cat` or `Get-Content`
  without calling `can_use_tool`, so `GUARD_HOOKS` also has a `PreToolUse` hook on Bash/PowerShell: `guard_shell`
  returns "ask" for anything `always_asks()` (secret folders, power, disks, hidden code), which routes it into
  `ask_before_risky`. Main and worker sessions share `GUARD_HOOKS`.
- **Reading tools:** `Read`/`Glob`/`Grep` never reach `can_use_tool` on their own; Claude Code approves them internally (verified). So the `guard_reads` `PreToolUse` hook returns "ask" when `search_root()` reaches a `SECRET_DIRS` folder (`~/.jarvis`, `~/.claude`, `~/.codex`, `~/.ssh`), which routes the call into `ask_before_risky` as an always-ask. `SECRET_DIRS` also holds `~/.caveman` and the Linux secret stores `~/.gnupg`, `~/.password-store` and `~/.local/share/keyrings` (on every OS, since they're harmless where absent). They're deliberately not in `AUTO_APPROVED`, so an allow rule can't shadow that "ask". The OS dirs are in `PROTECTED_DIRS` (no changes) but not in `SECRET_DIRS` (reading is fine).
- **Machines:** Jarvis has its own key `~/.ssh/jarvis_ed25519` and its hosts in `~/.ssh/jarvis_config`, which an `Include` line at the top of `~/.ssh/config` pulls in.
  - `add_machine` always asks. On approval it:
    - creates the key on first use;
    - writes a `Host` block with `BatchMode yes`, `IdentitiesOnly yes` and `StrictHostKeyChecking accept-new`;
    - pre-approves `host: <name>` and `host: <address>`;
    - prints and saves `WORKSPACE/setup-<name>.sh`. The user runs it once on the machine: it creates the user, gives passwordless sudo, and installs the key, restricted to this PC's IP via `from=` with no forwarding.
  - `list_machines` and `remove_machine` are auto-approved. `remove_machine` can't revoke the key on the remote side.
- Files: `in_free_zone()` allows; `is_protected()` always asks; otherwise trust mode can allow. File tools never get "always".
- Trust mode (`_trust_until`) is set only in `main()` from the user's own line via `trust_request()` ("stop asking", "stop asking me to run commands", "stop asking for N minutes" / "start asking again"; no length means `DEFAULT_TRUST_MINUTES`, 120; "stop asking about git commands" is not this, it's a lasting rule), never by the model, and is not persisted. Starting it approves waiting non-`ALWAYS_ASK` questions. It can't be started from `REDUCE_ONLY_SOURCES` (`sms`, `call`, `satellite`: caller ID can be faked, and anyone in the room can talk to a satellite); Jarvis says "You can turn that on at the PC or in the browser." Ending it works from anywhere.
- Every auto-approval beyond the read-only list prints `[auto-approved by <reason>: ...]`.
- Claude Code's own read-only command check can approve a shell command before `ask_before_risky` ever sees it;
  `guard_shell` (above) is what keeps that from reaching a secret folder.
- Everything else asks the user. The spoken summary for a shell command is the tool's model-written `description`; `parse_answer()` maps replies to `yes` / `no` / `always`, or `None` when the reply is neither. Only a clear no declines, because Whisper turns near-silence into words like "Thank you."; an empty text-mode line counts as a no. Questions never time out. The `sysadmin` subagent prompt is written to produce commands that pass this filter; change both together.

### Standing orders (agency.py)

- **Charter:** `~/.jarvis/charter.toml`, written by the user only (a secret folder; `charter.example.toml` explains
  every field). `[agency]`: `quiet_hours`, `max_worker_runs_per_day`, `max_actions_per_hour`, `ask_timeout_minutes`,
  `summary_at`. `[[order]]`: `name` (`[a-z0-9-]{1,32}`), `kind` (`backup|machines|mail|brief`), `schedule`
  (`daily HH:MM`, `weekdays HH:MM`, `every Nm|Nh`, at least 5 min), `check`/`probes` (watcher commands: must pass
  `is_read_only("Bash", ...)` and not be "ask" in `location_check`), `machines` (in `read_machines()`), `needs`
  (`grant_problems()`, machines also in `read_machines()`), `model` (`MODELS`), `allow` (servers and built-in tools
  for the worker); backup orders also `paths` (absolute; held to the check rules via `watchers.listing()`),
  `max_age_hours` (26), `min_size_mb`, and need paths or a check; machines orders need `machines`. Unknown keys are
  errors. A bad charter is reported once and the adopted orders carry on.
- **Adoption:** what runs is the adopted copy, `~/.jarvis/agency/adopted.json` (per-order sha256 of canonical JSON),
  never the file. New or changed orders and new limits wait for ONE hard question (`open_question(..., only=
  {"physical", "device"}, not_kinds={"satellite"})`: neither a text with its code, a call nor a voice satellite can
  answer; `Question.only` (trust levels), `Question.not_kinds` (channel kinds), `may_answer(q, by, kind)`);
  a newer charter closes it and asks again; a no is remembered for that charter. A yes runs `offer_grants` +
  `accept_grants` under `order-<name>`. A removed order's grants are dropped at once.
- **Scheduler** (`Agency.run()`, started by `main()` in `--core` only, cancelled on the way out): a tick every 30 s,
  local time, state in `~/.jarvis/agency/state.json`. A new order first runs at its next slot; a daily/weekdays slot
  missed by under 3 h is caught up, an older one is journaled as missed; one worker at a time. `control.paused()` is
  checked every tick and pausing cancels the running order (its open question is closed as a no).
- **A run:** `make_task()` builds an in-memory `tasks.Task("order-<name>", in_memory=True)` (never saved, listed or
  resumed) with one step, `worker=worker_kwargs(order)` for `jarvis.worker_options(servers=, builtin_tools=,
  disallowed=, agents={}, order=True)`: `ORDER_WORKER_PROMPT` (unattended, no subagents, spoken summary, terse
  evidence), no caveman style, and no subagent line in the step brief; mail and calendar writes are always
  disallowed. It runs `tasks.do_step(..., on_usage_limit="skip")` with `guardrails.current_task` and
  `guardrails.agency_run` (an `agency.Run`) set. First `WATCHERS[kind]` (`watchers.py`) looks: `None` means all is
  well (journaled with `watchers.FINE[kind]`, no worker); findings go into the worker's step after `KIND_BRIEFS[kind]`.
  A done report is said like task news (`tasks.announce`) unless it is `NOTHING_TO_TELL`.
- **The kinds** (`watchers.py`; commands via `run_command()`, no shell here, `ssh -o BatchMode=yes` on machines;
  ssh's exit 255 = unreachable, then that machine's other commands are skipped):
  - `backup`: each path's newest entry (`os.scandir` here, `ls -l --time-style=+%s` over ssh) must be newer than
    `max_age_hours` and not empty / under `min_size_mb`; `check`/`probes` must exit 0. Worker tools: machines, time,
    Read/Glob/Grep, shell.
  - `machines`: every check/probe (default `uptime`, `df -h`) on every machine; failures, unreachable machines and
    a `df` at 90% or more are findings. Tools: machines, time, shell.
  - `mail`: `imaplib` with `MAIL_HOST`/`MAIL_USER`/`MAIL_PASSWORD` (`mcp_launch.read_secrets()`, port 993): `EXAMINE
    INBOX`, `UID SEARCH UNSEEN`, `UID FETCH ... (BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])`; never a body, flag or
    `STORE`. Reported UIDs are kept in `~/.jarvis/agency/watch/<name>.json`, so only new unread mail wakes a worker.
    Tools: mail, time, with every mail write disallowed. `read_message` is not in `MAIL_READS` (its `mark_read`
    sets `\Seen` and can't be disallowed per argument), so workers read with `get_message_raw` (always EXAMINE).
  - `brief`: `brief_facts()`: tasks running/paused/failed/waiting (with their question), open questions, and the last
    day's journal (runs, `wants`, failures). With the calendar configured and allowed (default tools calendar, time),
    a worker adds today's events and its summary is the brief; otherwise `brief_text()` is announced at once.
- **Permissions inside a run** (`decide()`): no trust mode, no "always" rules and no safety check, so only read-only
  rules, the order's grants and the free zone allow; "always" counts as yes and saves nothing. `ask_user()` names the
  order, says no at once in quiet hours, and closes the question as a no after `ask_timeout_minutes`; trust mode never
  answers these (`Question.unattended`).
- **Budgets:** `max_worker_runs_per_day` counts worker sessions: `tasks.do_step(..., start_worker=Agency.start_worker)`
  asks before each one (a retry too) and ends the run as "over budget" when it's used up; `tick()` starts no run then,
  and a watcher that finds nothing starts no worker. `max_actions_per_hour` (every tool call by an order's worker, all
  orders together) is the `guard_agency` `PreToolUse` hook (matcher `None` in `GUARD_HOOKS`; no opinion outside a run).
- **Quiet hours and news:** reports, the daily summary and the worker-less brief go through `Agency.tell()` /
  `agency.tell()`: said like task news, except in quiet hours, when they wait in `~/.jarvis/agency/held.json` (kept
  across restarts, at most 50) and `deliver_held()` tells them together on the first tick after (paused or not). The
  user's own task news is never held.
- **Journal:** `~/.jarvis/agency/journal.jsonl` (moved to `journal.1.jsonl` past 5 MB), written by `JournalHandler`
  on the `jarvis.agency` logger: `agency.journal()` from the scheduler, `guardrails.agency_note()` from `auto_allow`,
  `ask_user` and `tasks._work`. An entry's `part` (`did`, `declined`, `wants`) puts it in the daily summary, said at
  `summary_at` with `Agency.tell()` (spoken or texted like task news, held in quiet hours, and put in front of the
  next message).
- **"Pause your standing orders" / "resume your standing orders"** (`agency.pause_request()`): read from the user's own
  lines in `main()`, never from the model, like trust mode; resuming isn't accepted from `REDUCE_ONLY_SOURCES` (a text,
  a call, a satellite), pausing is. Same switch as
  `jarvisctl pause`. `agency.py` is in `GUARDED_FILES`; `test_agency.py` stubs `tasks.run_worker`, `tasks.verify`
  and `agency.now` and clears `WATCHERS`; `test_watchers.py` covers the kinds (stubbed `run_command`, a scripted IMAP
  server on a socket pair), the phone and the charter question, `jarvisctl status` and the order prompt.
  `watchers.py` is in `GUARDED_FILES` too.

### Concurrency / I/O

- `listen()` runs as a background task, speaking main-agent text as it streams in (subagent text is not spoken; tool uses are printed).
- Permission questions can arrive mid-conversation. They queue in `_pending_questions`; the user's next line or utterance answers the oldest one instead of going to Claude. If the reply is neither yes nor no, the question stays open: under four words is treated as noise, and anything longer goes to Claude as a normal message. On exit, all pending questions are declined.
- `_io` (and `guardrails._io`) is the `hub.Hub`, which routes between its channels (`TextIO`, `voice.Voice`, `web.WebClient` for faces, terminals and satellites, `phone.Channels`). Channels are duck-typed (the hub's docstring lists the interface: `say()`, `ask(summary, detail)`, async `hear()` or pushed lines, ...). Keep them in sync.

## The core as a service (control.py, jarvisctl.py, terminal.py)

- **`--core`** (the default; `--voice` and `--text` are the PC's own mic or keyboard): no keyboard, mic or speakers at the PC. Jarvis is reached
  through the interface's sockets (browser faces, `jarvisctl attach` terminals) and the phone. On Linux, SIGTERM (a
  service stop, logging out) sets the main loop's `stop` event for a clean shutdown. Under `pythonw` (the Windows
  task) there's no console, so output goes to `~/.jarvis/logs/console.log`.
- **Exit codes** (`run_jarvis.py` acts on them):
  - `RESTART_EXIT_CODE = 3` (`updater.py`, copied in `run_jarvis.py`): start again at once (an applied update,
    `jarvisctl restart`, or Jarvis restarting himself).
  - **Restarting himself:** `mcp__updates__restart_yourself` (auto-approved; `updater.request_restart()` sets
    `restart_requested` and `restart_reason`, so `listen()` restarts after that reply), and `jarvis.Health`:
    `FAILED_TURNS_RESTART` (2) turns in a row whose error matches `CONNECTION_FAILED` (a stale proxy or
    connection; a restart also restarts Caveman via `detect_caveman()`), or `watch_health()` finding a turn with
    nothing from Claude for `STUCK_S` (20 min) and no question open, call `restart_now()`. The log says why.
  - `LOST_CLAUDE = 75` (`jarvis.py`): the connection to Claude dropped; treated as a crash.
  - 0 and `STOPPED_BY_USER` (Ctrl+C, closing the window, SIGHUP 129, SIGTERM 143 / -15): stopped on purpose, never
    restarted or rolled back. So `jarvisctl stop` stays stopped, and the systemd unit counts 143 as success.
  - Anything else: a rollback if `ROLLBACK_FILE` exists. Otherwise, for the core only, a crash restarted after
    `BACKOFF_S` (5 s, 30 s, then 2 min); `CRASH_LIMIT` (5) crashes within `CRASH_WINDOW_S` (10 min) and the supervisor
    gives up, leaving `NOTE_FILE` for the next start. Voice and text mode crashes aren't restarted.
- **Control** (`control.py`, stdlib only, since jarvisctl imports it): every start writes a fresh token to
  `~/.jarvis/control.token` (0600 where the OS allows; a secret folder, so Jarvis's own tools can't read it).
  `/control/<command>` on the interface's server needs `Authorization: Bearer <token>` and no Origin header (never a
  page), on top of the `Guard`'s loopback checks. `jarvis.control_commands()` registers what it answers: `status`,
  `pause`, `resume`, `standby`, `stop`, `restart`, `pair`, `devices`, `revoke`, `reset_devices`.
- **`jarvisctl.py`** (stdlib only except `attach`; honours `JARVIS_WEB_PORT`; over SSH: `ssh host python3 jarvisctl.py status`):
  - `status`: pid, uptime, standby, paused, trust mode, Codex and Caveman, the channels, open questions, tasks,
    and each standing order (`agency.status()`: adopted / changed / waiting / declined, last run, next run) plus any
    charter problems.
  - `pause` / `resume`: standing orders off / on. The kill switch is the file `~/.jarvis/agency/paused`
    (`control.PAUSED_FILE`, `control.paused()`). If the core doesn't answer, jarvisctl writes or removes the file
    itself, so pausing works even when the core is down or stuck. Anything acting on its own must check it.
  - `standby on|off`, `stop`, `restart`.
  - `rules`, `allow PATTERN`, `always-ask PATTERN`, `trust-folder PATH`, `ask-folder PATH`, `remove N|VALUE`: edit
    `~/.jarvis/permissions.toml` directly through `permissions.py` (rewritten as clean commented TOML, other entries
    kept, created if missing), so they work with the core down; a running core rereads it on its next decision.
    jarvisctl checks trusted folders against `permissions.basic_protected()`; the core checks again with `PROTECTED_DIRS`.
  - `attach [--name NAME]`: talk to Jarvis in this terminal (`terminal.py`, needs `websockets`).
  - `pair [--name NAME] [--kind face|satellite]`: a one-time code for a browser (default; prints the `#pair=` URL) or a
    satellite (prints `satellite.py --pair CODE`); a device is labelled with the kind that really paired.
    `devices [list | revoke ID | reset]`.
  - `satellite-bundle DIR`: copies `SATELLITE_FILES` (`voice.py`, `protocol.py`, `satellite/satellite.py`,
    `requirements.txt`, `README.md`, in that layout) into a new or empty folder outside the project, to copy to the
    satellite machine. It never overwrites.
  - `install-service` / `uninstall-service` (below).
- **Service install**:
  - Linux: a systemd user unit, `~/.config/systemd/user/jarvis.service`, running `run_jarvis.py --core` with
    `Restart=on-failure`, `SuccessExitStatus=143` and the PATH jarvisctl ran with (a user service otherwise gets a
    minimal PATH, and claude, codex, cloudflared and caveman wouldn't be found); then `daemon-reload` and
    `enable --now`. It suggests `loginctl enable-linger` so Jarvis starts at boot and survives logging out. Logs:
    `journalctl --user -u jarvis`. `systemd_unit()` always writes POSIX paths (the project path is a parameter), so
    it's testable on Windows.
  - Windows: a Task Scheduler task, `Jarvis`, at log-on as the user (a real Windows service couldn't use the Claude
    login or the audio devices), run with `pythonw.exe` (no console; `run_jarvis.no_window()` hides the children's),
    with no time limit, not stopped on battery, one copy at a time, restarted on failure. The XML is written to
    `service/jarvis-task.xml` (UTF-16) and registered with `schtasks /Create /XML`; `schtasks /Run /TN Jarvis` starts it now.
- **`jarvisctl attach`** (`terminal.py`): a protocol client of kind `terminal`, with no display. Its hello carries
  the control token, so `WebServer._introduce()` gives it `trust="physical"`, the standing of the keyboard at the PC
  (it can answer any question and switch trust mode); its `source` is `terminal`, so the PC's quit words don't stop
  Jarvis from it. It gets `console` frames (`hub.console()`: auto-approvals, notices), which faces don't. Typed lines
  come from a daemon thread, so a waiting `input()` never keeps it alive; it ends when the core closes the socket, and a
  refused hello (a stale token after a restart) is an error, exit 1. A socket with no Origin and no valid token is
  closed with 4003.

## Architecture (web.py: the browser interface)

`--core` serves the interface: each socket is a `web.WebClient`, a channel of the hub like any local one, so
guardrails, tasks, phone and updates are unchanged. Nothing is spoken on the PC.

- **Hearing** (`/stt`, `speech=True` from `web_channel()`): the page's voice-activity detector records each spoken
  segment (WebM/Opus) and posts it here; `decode_audio()` turns it into 16 kHz samples with PyAV (not faster-whisper's
  own decoder, which breaks with PyAV 19) and the voice layer's Whisper transcribes it, loaded in the background at
  startup. `/health` then reports `stt: true`, which switches the page to that path (`startElevenVoice` in
  `web/src/lib/voice.ts`, named for the original's ElevenLabs back end). The wake word is matched in those
  transcripts: his name (or a common mishearing) anywhere, with an optional greeting ("Morning Jarvis", "Jarvis, you
  there?", "check the NAS, Jarvis"), and `stripCall()` keeps the rest as the command. `App.tsx` uses the same
  helper to drop his name from a command while already listening. Voice mode's wake word is openWakeWord's
  `hey_jarvis` model, so it stays "Hey Jarvis".
- **Holding and text mode** (`web/src/lib/hold.ts`, pure and tested with `npm test`): while he's speaking aloud
  (`tts.audible()`: any speaker with audio playing, being generated or queued; or the `speaking` phase, unless a
  permission question is open) the voice loop is in mode `hold`: speech is dropped (a segment begun then is dropped
  however late its transcript, before `/stt` is called), nothing barges in. A line typed meanwhile is held (shown
  above the box) and sent once he's quiet and the turn is over, or quiet with a question open; a typed yes / no /
  always answers the oldest open question at once (`answer` frame). STOP beside SEND and Escape (also inside the box)
  cut him off and `interrupt()` the server turn. After a typed turn the page is in text mode: no follow-up mic, it goes
  dormant (waiting for his name), and a busy page hears only his name; saying it, Space, or a spoken follow-up
  returns to voice.
- **Standby** (`standby_request()`): the phrases may be said casually, with courtesy words and his name around them
  ("Okay Jarvis, wait for further instructions", "that'll be all, thanks", "dismissed"); the whole line must be the
  request. On a face, `WebClient.standby = True` sends `{"type": "standby"}`, and the page goes dormant (waiting for
  its name) once "Standing by." has been spoken, instead of opening the mic for a follow-up. Being called by name
  wakes it again. Without faster-whisper the page falls back to the browser's own recogniser, which works only in Chrome
  and Edge: Brave and Chromium fail every session with `network` (the page now says so), Firefox has none. Whisper
  (`speech.py`) uses beam search (`beam_size=5`) and hint words: "Jarvis" (a short "Hey Jarvis" alone often comes
  back as "Javis") plus `jarvis.speech_hints()`, the free zones and trusted folders, the projects inside the ones
  that aren't git repos themselves, and the machines (at most `HINT_LIMIT`). Each segment heard is logged
  (`jarvis.speech: heard 2.1s: '...'`), so misheard or missed speech can be checked in the log.
- **Speaking** (`/tts`): the page posts each sentence as `{text}` and plays what comes back: ElevenLabs' MP3 when
  it's set up (`elevenlabs.py`, below), else a WAV in the voice layer's Kokoro voice, with paths, links and dates cleaned by `voice._clean_for_speech()`. `/health` reports
  `tts: true` when kokoro-onnx is installed; otherwise the browser's `speechSynthesis` speaks, which has no voices in
  Brave or Chromium on Linux. Both models load in the background at startup.
- **On another machine**: run Jarvis (`--core`) on the stronger PC, Windows or Linux, and reach it from the laptop
  through SSH: `ssh -N -L 8790:127.0.0.1:8790 user@host`, then open http://localhost:8790. Jarvis stays bound to
  127.0.0.1, and the page counts as localhost, which browsers require for the microphone. A different laptop port
  works too: `same_origin()` accepts a page whose Origin is exactly the loopback Host it talks to. Jarvis prints
  the command at startup. Everything Jarvis does then happens on that machine (its free zones, its shell, its
  sysadmin checks), and `web/` must be built there. The host needs an SSH server: OpenSSH Server on Windows
  (`Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, then `Start-Service sshd`), `openssh-server`
  on Linux.

- **Who may connect** (`allowed()`, ASGI `Guard`): it listens on 127.0.0.1 only. Every request needs a loopback Host
  (stops DNS rebinding). A socket with an Origin needs one of the interface's own (Jarvis's port, or Vite on
  5173-5199/4173-4199); a socket with none (a terminal, a satellite) gets through the Guard but must show a token in
  its hello. Plain requests may come without an Origin (an `<img>` sends none) but are refused with a foreign one or
  `Sec-Fetch-Site: cross-site`. The Guard answers CORS preflights from the Vite origins. `web.py`, `protocol.py`,
  `devices.py`, `terminal.py` and `satellite/satellite.py` are in `GUARDED_FILES`.
- **Clients, pairing and tickets** (`protocol.py`, `devices.py`, `WebServer._introduce()`):
  - The first frame is `hello {protocol: 1, client {kind face|terminal|satellite, name}, caps {display, gestures,
    speech}, token?, pair?}`; Jarvis answers `welcome {client_id, device?, token? (just paired), media_ticket?,
    state {standby, questions, screen}}`, then `ready`. Close codes: 4001 revoked/unknown device, 4002 wrong protocol
    version (also a first frame that isn't a hello), 4003 pairing required. Version 1 is frozen (`protocol.py`'s
    docstring says how a v2 would come in).
  - Open mode: until a device is paired (`Devices.enforced`), any page of the interface on this PC connects.
  - Pairing: `jarvisctl pair --name laptop` (`--kind satellite` for a satellite) makes a one-time code (6 digits, 10 min, dropped after 5 wrong tries;
    one at a time) and prints `http://localhost:8790/#pair=CODE`. The page reads the fragment (never sent to a server),
    clears it, sends `pair` in its hello and keeps the token it gets in `localStorage.jarvisToken`. Only the token's
    sha256 is stored, in `~/.jarvis/devices.json` (a secret folder). The `--name` wins over the client's own name.
  - The control token in a hello makes a terminal with `trust="physical"`; a device token gives `trust="device"`.
    `jarvisctl devices revoke ID` closes that device's sockets at once (`close_device()`, 4001); `devices reset`
    is back to open mode.
  - Media tickets: once paired, `/file`, `/img` and `/media` need `?mt=<ticket>` (an `<img>` can't send headers) and
    `/stt` and `/tts` need `Authorization: Bearer <ticket>` (`_ticket_ok()`). A ticket is bound to its device,
    valid 12 h, and replaced at every hello and every `ticket` frame (answered `{type: ticket, media_ticket}`); the
    replaced one works 5 more minutes, and tickets the device's other open sockets hold (another tab) are left alone.
    The face renews every 6 h, and on a 403 (`fetchWithTicket()` for `/stt` `/tts`, a capturing `error` listener for
    pictures and video, the orbits' loader) renews once and retries. `/health` needs none.
  - Code that compares a client's token or code uses bytes (`hmac.compare_digest` raises on non-ASCII str).
- **Protocol** (after the hello, as `web/src/lib/bridge.ts` speaks it): the page sends `ask {text, id}` (ids carry a
  random per-page prefix), `interrupt`, `answer {q, decision}` (a question card's yes/no/always button) and
  `presence` (focus, visibility, pointer or hands; at most every 10 s). Jarvis sends `text`/`tool`/`done` tagged with
  the question's id, `say` for speech between turns, `question` / `question_closed`, `blade` and `ui` frames. Only
  the page's latest question is still listened for (`live`); speech for anything else goes out as `say`, which the
  page speaks on its own (`announce()` in `App.tsx`). The page redials forever (backoff capped at 30 s), except after
  4001/4002/4003, which it shows as a message.
  - Several screens at once: each is a channel; Jarvis speaks on, and the interface tools draw on, the one used most
    recently (`hub.primary()`, `hub.display_target()`). `hub.Scene` keeps the look, the orbits and up to 20 blades
    (not question cards), and a screen that connects is sent them (`scene.frames()`), so it carries on from there.
    When the screen in use changes, `jarvis.prompt_for()` starts Claude's next message with `[Screen: <name>]` (or
    `[Screen: none]`), once per change; with no screen, `ui_tools` refuses and says to speak or text a link.
  - `main()` calls `_io.turn_started()` just before `client.query()`. A line that never starts a turn (a permission
    answer, standby or trust phrase, noise) gets its `done` when `main()` comes back to `hear()`, and Jarvis's own
    reply to it (`set_trust`'s acknowledgement) goes out as that question's answer.
  - Permission questions show as a markup blade with the exact command, and are spoken. Talking over an open
    question answers it: `interrupt` is ignored while `_pending_questions` is non-empty. Otherwise `interrupt` calls
    `client.interrupt()`, and the next line waits up to `SETTLE_S` for that turn to end so answers don't cross.
  - The conversation outlives the socket: a reconnecting page carries on, and is re-asked any open question.
    Every screen shows each open question; it's spoken only on the primary one.
  - `source` is `"web"`, so the PC's quit words don't stop Jarvis from a browser tab.
- **Interface tools** (`ui_tools.py`, registered in every mode, auto-approved, refused with no screen, never given to task
  workers): `ui_theme`, `ui_reactor`, `ui_orbit`, `ui_chrome`, `ui_effect`, `ui_screen`, `ui_reset`, `display` and
  `blade`. Each pushes one frame; loose input is coerced and clamped; empty requests are refused. Not ported:
  article blades (the face's `/page` reader view was removed: there's no sanitised-page server), the camera,
  `probe_url`, browser control.
- **Media** (`/file`, `/img`, `/media`): the page loads pictures and video only through Jarvis.
  - `/file`: image types only (no SVG), symlinks resolved first, inside home, `WORKSPACE`, the free zones or temp
    (anywhere when `CONTAINED`), never in `SECRET_DIRS`; 25 MB cap. `/C:/x` (from `file:///C:/x`) is accepted.
  - `/img` and `/media` go through `netguard.py`: http(s) only, no LAN names, the host resolved once and refused if
    any answer isn't public, the connection made to the checked address (`GuardedBackend`), every redirect
    re-checked, no environment proxy. Only `image/*` or `video/*`/`audio/*` comes back, never XML or SVG, with size
    caps, Range passed through for `/media`.
  - Everything served carries `nosniff` and a `sandbox` CSP: a document served from this origin could open the
    socket.
- **Front end** (`web/`): a built page (`web/dist`, served by Jarvis) talks to the host it came from; the Vite dev
  server talks to port 8790 on the same hostname. Disk paths (POSIX roots and Windows drives) become `/file` URLs
  through one helper, `localFileUrl()` in `web/src/config.ts`, which (like `withTicket()` for `/img` and `/media`,
  and `fetchWithTicket()` for `/stt` and `/tts`) adds the media ticket. The CSP in `web/index.html` allows pictures,
  video, frames and fetches from `'self'` only; a dev-only plugin in `web/vite.config.ts` adds `localhost:8790` back
  for `npm run dev`. `web/src/ui/sanitise.ts` (DOMPurify, class allowlist) and `web/index.html` are in
  `GUARDED_FILES`. Build: `cd web && npm install && npm run build` (`tsc -b && vite build`).

## ElevenLabs (elevenlabs.py)

- **On** when `~/.jarvis/secrets.env` has `ELEVENLABS_API_KEY`. Read from there only, never the environment, which
  Jarvis's shell commands inherit. Optional `ELEVENLABS_VOICE_ID` (default George, `JBFqnCBsd6RMkjVDRZzb`) and
  `ELEVENLABS_MODEL` (default `eleven_flash_v2_5`; `eleven_multilingual_v2` is richer but slower).
- **Where:** the browser interface's `/tts` (the MP3 goes to the page as is) and the voice layer's `_synthesizer()`
  (decoded to 24 kHz with `voice.decode_audio()`). Phone calls keep Kokoro.
- **Fallback:** anything that fails falls back to Kokoro for that sentence; a bad key, no credits or rate limiting
  (401/402/403/429, "quota") pauses ElevenLabs for `PAUSE_S` (30 min). Format `mp3_44100_128`, the default on
  every plan.
- **Privacy:** every sentence Jarvis says is sent to ElevenLabs, mail it reads out included.

## Architecture (voice.py)

Mic (16 kHz, 80 ms frames) → openWakeWord `hey_jarvis` → Silero VAD (from openwakeword) records until 0.8 s silence → faster-whisper (`large-v3-turbo` on CUDA, falls back to `small.en` CPU) → text. Replies → Kokoro ONNX TTS (`bm_george`, en-gb).

- `_load_tts()` puts Kokoro on the GPU via `onnxruntime-gpu` (CUDA 13; `rt.preload_dlls()` loads the `nvidia-*` cu13 wheels), else the CPU, and warms it up. Whisper's ctranslate2 uses the separate CUDA 12 wheels. Both cuDNN builds share the file name `cudnn64_9.dll`, but loading them in either order was verified to work. The `pip check` complaint that `onnxruntime` is missing is expected.

- Speech engines: `Voice(spoken_paths, stt=None, tts=None)`. An engine has the local model's own interface (Whisper's
  `transcribe(audio, language=, beam_size=)` → `(segments, info)`, Kokoro's async `create_stream(text, voice=, lang=)`),
  so the defaults are the models themselves (`_load_whisper()`, `_load_tts()` + ElevenLabs), and `_stt`/`_tts` stay what
  `jarvis.call_models()` hands phone calls. `RemoteSTT`/`RemoteTTS` (sharing one `RemoteSpeech`: base URL, `ticket`,
  `on_refused`) post a 16 kHz WAV to the core's `/stt` and each line (≤ 1800 characters at a time) to `/tts`, with
  `Authorization: Bearer <ticket>`; a 403 calls `on_refused()`. `RemoteTTS.cleans_text`: the core says paths and dates
  with its own folder names, so `_synthesizer()` doesn't clean first. With a remote `tts`, Kokoro isn't downloaded or
  loaded. `on_barge_in` is called when the wake word cuts Jarvis off.
- Speech is two tasks. `_synthesizer()` turns queued `(text, show)` lines into audio chunks, running ahead into `_audio` (maxsize 4), and ends each line with a `samples=None` marker. `_player()` plays the chunks. Kokoro only pipelines within one line, so this split is what removes the pause between lines.
- `say()` doesn't print. The player prints each line as it starts playing, so the screen keeps pace with speech. `_interrupt()` prints any cut-off lines as `jarvis (not spoken)>` in order: queued audio, then `_in_flight`, then queued text.
- `_lines` counts lines not yet finished. `_busy_speaking()` and the follow-up window depend on it.
- `_speak_dates()` (inside `_clean_for_speech()`) is a speech-only backstop. It expands "Wed, Nov 11" and ISO dates and adds ordinals, but only in date context, so "Jan" the name and "wed" the verb stay.
- `_clean_for_speech()` shortens paths for speech only. `PATH_TOKEN` finds them and `_speak_path()` says each as its folders separated by commas, naming `jarvis.py`'s `SPOKEN_PATHS` (workspace → "the workspace", home → "home") and drives ("C drive").

- Wake word during speech interrupts TTS (`_speech_generation` counter cancels in-flight speech).
- Standby: when `Voice.standby` is set there's no follow-up window. Only the wake word opens the mic, permission answers included, and `ask()` says so. `jarvis.py`'s `standby_request()` and `set_standby()` switch it from the user's own line, never from the model. They speak a fixed acknowledgement with no Claude turn and persist the state in `~/.jarvis/standby`.
- Follow-ups: whenever Jarvis finishes talking (queue empty, not interrupted), `_follow_up` opens the mic without the wake word for `NO_SPEECH_TIMEOUT_S`. This also covers answering `ask()` questions.
- Turn-taking: `_mic_free` is cleared while you talk (from the start for wake-word turns, from first detected speech for follow-ups); the speaker waits on it so the open mic never records Jarvis. In a follow-up where you haven't spoken yet, new Jarvis speech cancels the recording instead.
- Kokoro model files download into `./models` on first run; openWakeWord models download via its utils.
- `_add_cuda_dlls()` makes the `nvidia-*` pip wheels' cuBLAS/cuDNN reachable for faster-whisper: on Windows it registers their DLL dirs; on Linux it loads their `.so` files with `RTLD_GLOBAL` (cuBLASLt first), since the loader reads `LD_LIBRARY_PATH` only at startup. Untested on a Linux GPU; without a working CUDA it falls back to `small.en` on the CPU.
- `sounddevice` is imported on first use (`_SoundDevice`), so the speech helpers and phone calls work without PortAudio, and `Voice()` stops at once with install instructions (`PORTAUDIO_MISSING`) if it's missing.

## Voice satellite (satellite/satellite.py)

A mic and speaker for Jarvis elsewhere: a protocol v1 client of kind `satellite` (caps `display: false`,
`audio_in: segments`, `audio_out: server`) around a `voice.Voice` with the remote engines. Own requirements
(`satellite/requirements.txt`: no Whisper or Kokoro); it imports `voice.py` and `protocol.py` from its parent folder,
so `jarvisctl satellite-bundle DIR` copies exactly those files, in that layout, for another machine.
`satellite/README.md` is the user's guide.

- **Pairing:** `jarvisctl pair --kind satellite --name kitchen`, then `satellite.py --pair CODE` (the hello sends the code, not an older
  token). The token from the welcome goes to `token_path()`: `%APPDATA%\jarvis-satellite\token` on Windows, else
  `$XDG_CONFIG_HOME` or `~/.config`, written 0600; never the project. `--url` (default `ws://127.0.0.1:8790/`, the
  core only listens on loopback: from another machine through `ssh -L`), `--name`, `--token-file`.
- **On the core:** a `WebClient` with `kind`/`source` `satellite`, `trust="device"`, no display (so no `ui_tools`
  target). The hub speaks to it like any channel (`text` frames for its own line, `say` between turns);
  `hub.notify()` uses a channel's `notify()`, which `WebClient` sends as `say` with `news: true`, never mixed into an
  answer. `show_question()` sends a satellite only the question being asked (the oldest), and never one it can't
  answer (`not_kinds`): for that it says `guardrails.waiting_elsewhere()` once per question
  (`WebServer.told_elsewhere`). Ordinary questions it may answer. `hub.kind` is the kind the line came by, and the
  main loop passes it to `answer_oldest_question()`; from where the oldest question can't be answered, a short yes or
  no is told where it waits and anything else goes on to Jarvis (not held as noise).
- **Loop:** each utterance (local wake word + VAD) goes to `/stt`; with a question open, a yes/no/always
  (`parse_answer()`, the core's words) is sent as `answer {q, decision}` for the oldest, anything else as
  `ask {text, id: sN}`. `text`/`say`/`notify` frames are spoken through `/tts`; `question`/`question_closed` keep
  `questions`; `standby` sets `Voice.standby`, and a line heard in standby (after the wake word) clears it, as a face
  does. The wake word over Jarvis's speech sends `interrupt`.
- **Connection:** `run()` redials with `backoff(attempt)` (1, 2, 4 ... capped at 30 s; back to 1 s after a welcomed
  session); a refused ticket (403, e.g. after 12 h) closes the socket for a new hello. 4001, 4002 and 4003 print
  `STOP_MESSAGES` and stop (exit 1); so does starting with no token and no code.
- `tests/test_satellite.py`: a real `WebServer` with a fake `Speech`, openWakeWord, `sounddevice` and `_beep` stubbed
  in `sys.modules`/the module, frames fed straight into `Voice._frames` (a frame's value says wake, speech or quiet).
