# Handoff: Jarvis, the independent core and its clients (Phases 0–7, all done)

Jarvis is one always-on **core** (`python run_jarvis.py`, which is `--core`) that owns the conversation with Claude,
the permission layer, long tasks and standing orders. People reach it through clients: the browser **face** (`web/`),
**terminals** (`jarvisctl attach`), **voice satellites** (`satellite/`) and the **phone** (Twilio), all channels of one
hub, speaking the frozen client protocol v1 with paired devices and media tickets. `--voice` and `--text` still run
Jarvis at this PC's own mic or keyboard. CLAUDE.md describes the finished system ("Architecture" first).

Branch `main`. Everything listed is committed and passes `python -I tests/run.py` on Windows 10, Python 3.13 (and
on Linux, Python 3.12, up to Phase 2). The face builds with `cd web && npx tsc -b && npx vite build`.

## Phases

| Phase | What | Commits |
|---|---|---|
| 0 | Bug fixes (double speaking, echo, SMS/trust holes, tab eviction) | `09f57fd` |
| 1 | Channel hub (`hub.py`, questions with ids, phone as a channel, `WebServer` + `WebClient`, `speech.py`) | `3b90a7c` |
| 2 | Core service (`--core`, `console()`, SIGTERM, exit 75, supervisor backoff, `control.py`, `jarvisctl.py`, service install) | `1c57cc8`, `c038f6a` |
| 3 | Protocol v1, devices, pairing, media tickets, display routing, the face on v1 | `7d2e3c7`, `a4e3e50`, `f5d8d64`, `cd78a77`, `49fa90a`, `03ec1bf` |
| 4 | Agency foundation (`agency.py`: charter, adoption, scheduler, unattended permissions, budgets, journal, summary) | `567686e`, `3d765c2` |
| 5 | The four standing orders (`watchers.py`: backup, machines, mail, brief), phone/charter fix, orders in status, order prompt | `e58a862`, `6cb0507`, `8c526e7`, `a452f31`, `58763b9`, `960c0f4`, `8226064`, `4d1f9c5`, `cb02cdb` |
| 6 | Voice satellite (`voice.py` engines, satellites and the charter, `satellite/satellite.py`) | `e0e8526`, `f85066c`, `c4d9a6c`, `b2b9675` |
| 7 | Cleanup (below) | `09c4910`, `06ad399`, `c5d91a3`, `8531675`, `84ba31b`, `ac5440f`, `5dad59a`, `d3dfd7c`, docs commit |

### Phase 7, what's in
- `09c4910`: no mode flag = `--core` (`run_jarvis.is_core()`, the same rule in `jarvis.main`); `--voice` is the PC's
  mic and speakers; `--web`/`--headless` are gone and unknown flags are refused (exit 2) before Jarvis starts. Protocol
  v0 is gone: a first frame that isn't a hello is 4002 for everyone. `protocol.py` says v1 is frozen and how a v2
  would come in; `test_protocol.py` pins the constants, the hello's defaults and the welcome's shapes. The service
  install already ran `run_jarvis.py --core`, so it didn't change.
- `06ad399`: in quiet hours, order reports, the daily summary and the worker-less brief wait in
  `~/.jarvis/agency/held.json` (`Agency.tell()`, `agency.tell()`) and are told together on the first tick after
  (`deliver_held()`, paused or not; a restart keeps them). User task news is never held.
- `c5d91a3`: `max_worker_runs_per_day` counts worker sessions: `tasks.do_step(start_worker=)` asks before each one, a
  retry included; past the budget the run ends "over budget". A watcher that finds nothing starts no worker, so counts
  none.
- `8531675`: a `ticket` frame renews the media ticket without a new hello (`{type: ticket, media_ticket?}`). The face
  renews every 6 h and, on a 403 for `/stt` `/tts` (`fetchWithTicket()`) or a picture or video (a capturing `error`
  listener; the orbits' texture loader), renews once and retries. A device's new ticket no longer shortens the tickets
  its other open sockets hold.
- `84ba31b`: texts, calls and satellites (`jarvis.REDUCE_ONLY_SOURCES`) can't turn trust mode on or resume standing
  orders ("You can turn that on at the PC or in the browser."); trust off, pausing and standby work from anywhere.
- `ac5440f`: `jarvisctl pair --kind face|satellite` (default face) prints the URL or the satellite command; the
  control command refuses other kinds; a device is labelled with the kind that paired (a mismatch is logged).
- `5dad59a`: the face's `/page?mode=reader|live` article blades were removed. They were framed from the upstream Node
  bridge's `bridge/page.mjs`, which was never ported; `web.py` has no `/page`, there's no sanitised-page server to back
  one (`netguard.py` only fetches images and video), and `ui_tools` never sends an article blade. Serving third-party
  documents from Jarvis's origin would also put them next to the socket, so implementing it wasn't safe to do here.
- `d3dfd7c`: `jarvisctl satellite-bundle DIR` copies exactly `voice.py`, `protocol.py` and `satellite/{satellite.py,
  requirements.txt, README.md}` into a new or empty folder outside the project (never overwriting; the live tree stays
  clean). The test runs the bundled satellite.
- Docs: CLAUDE.md's architecture rewritten for the finished system; this file.
- Bugs found and fixed: a second tab of the same browser (same device token) cut the first tab's media ticket to 5
  minutes, after which its pictures and speech failed until a reload; a text or call couldn't turn trust mode *off*
  ("start asking again" was refused like turning it on); `jarvisctl pair` told terminals to use `--pair` (they use the
  control token).

## Manual checks still needed

Nothing here could run a browser, real audio, another machine or the outside services. In one place:

- **Browser (the face)**
  - `jarvisctl pair --name laptop`, open the printed URL: the face connects, `#pair=` leaves the address bar,
    `localStorage.jarvisToken` is set, `jarvisctl devices` shows "laptop" (face); a reload reconnects with the token.
  - Another browser (or a private window) is refused with the "isn't paired" message and doesn't redial;
    `jarvisctl devices revoke ID` shows the "revoked" message at once.
  - `/file?path=…` without `mt` is 403; pictures in blades and orbits, `/img` thumbnails and `/media` video load.
  - Hearing and speaking with server speech (`/stt`, `/tts` with the bearer ticket); `npm run dev` too (the CORS
    preflight and the dev CSP).
  - A permission question: the card's yes / no / always buttons answer it, and the card comes down on every tab.
  - Gestures still move blades; two faces at once, Jarvis drawing on and speaking from the one used last.
  - Two tabs of the same browser both keep their pictures and speech past 5 minutes.
  - A tab open over 12 hours (or with the ticket expired by hand in `devices._tickets`): pictures, video, `/stt` and
    `/tts` keep working (the 6-hourly `ticket` frame, and the 403 retry).
  - Stop and start the core: the page redials and carries on (backoff up to 30 s).
- **Real mic and speakers**
  - `--voice` at the PC: wake word, follow-ups, barge-in, standby.
  - A satellite: wake word, end of speech, the follow-up window, barge-in, the beep; with the core's real Whisper and
    Kokoro or ElevenLabs (an ElevenLabs MP3 needs PyAV on the satellite).
  - Saying "stop asking for 30 minutes" or "resume your standing orders" to a satellite is refused aloud; "start
    asking again", "pause your standing orders" and standby work.
- **Another machine**
  - `jarvisctl satellite-bundle DIR` → copy DIR → `pip install -r satellite/requirements.txt` → `jarvisctl pair --kind
    satellite --name kitchen` → `satellite.py --pair CODE` through `ssh -L 8790:127.0.0.1:8790`; `jarvisctl devices`
    shows it (satellite); `devices revoke` stops it at once; stopping the core → it redials and carries on; a satellite
    running more than 12 h (a new hello on the 403).
  - The face from a laptop through `ssh -N -L 8790:127.0.0.1:8790`.
  - `jarvisctl install-service` on Linux (systemd user unit, linger) and Windows (Task Scheduler); on Windows it
    writes `service/jarvis-task.xml` into the project (git-ignored since `0e5a7c8`, so self-updates still start).
- **Real ssh, IMAP and NAS** (standing orders)
  - backup and machine-watch orders over `ssh -o BatchMode=yes` to real machines; `ls --time-style=+%s` on a NAS
    without GNU ls (it shows up as a finding, not silence).
  - mail triage against the real IMAP server (EXAMINE only, nothing marked read).
  - A real charter adopted at the PC or in the browser (not from a satellite or a text), a run in quiet hours whose
    report is told when they end, and the daily summary.
- **Twilio (the phone)**
  - Texts and calls both ways, the call PIN, coded answers ("YES 1234"), the charter question's single plain text, a
    missed call parking Jarvis, and order news held through quiet hours instead of texted at night.
  - "Stop asking for 30 minutes" or "resume your standing orders" by text or on a call is refused; "start asking
    again" and "pause your standing orders" work.

## Updates after Phase 7 (branch `updates-vault-browser-voice`)

| What | Commit |
|---|---|
| Face: hold what's typed or said while he speaks; text mode (no follow-up mic after typing) | `e76a8db` |
| Knowledge vault in Obsidian (`knowledge.py`, the librarian) | `9a34a65` |
| Jarvis's own browser (Playwright MCP behind `netguard.FilteringProxy`), "show/hide the browser" | `2eb4293` |
| No pause between sentences (the next one is made while the current one plays; one kept-open ElevenLabs connection) | `a4c022c` |
| The browser's live view on the face (`browser_view.py`, `/browser/live`), instead of a window on the PC | `18eab17` |

Checked here: the Python tests (`test_knowledge.py`, `test_browser.py`), `npm test` and the build; a real Chromium
through `browser_launch.py` refused 127.0.0.1, localhost, [::1], 192.168.1.1 and 169.254.169.254 (403), blocked
`file://`, and loaded https://example.com. `test_watchers.py` fails on Linux on `main` too (a backup path under
`/home/u/.ssh` isn't refused there); not touched by these updates.

Manual checks:
- **Face**: type a question: after the answer the mic doesn't open and the HUD waits for "Hey Jarvis"; saying it
  goes back to voice with follow-ups. While he speaks: talking doesn't interrupt, a typed line shows "HELD" and goes
  when he's done; STOP and Escape (also in the box) cut him off. A permission question: type "yes" while he's still
  asking; by voice, answer once he has finished asking.
- **Browser**: "show the browser" puts a live BROWSER blade on the face (from the laptop through the tunnel too);
  ask Jarvis to look something up in it and watch it move; asked to open `http://127.0.0.1:8790` or the router,
  the page is a 403; "hide the browser" takes the blade down; "let me watch" has Jarvis open it himself.
  Jarvis browses a public site; asked to open `http://127.0.0.1:8790` or the router, the page is a 403; "hide the
  browser"; a login made in it survives the switch. A restart (`reconnect_mcp_server`) mid-turn waits for the turn.
- **Vault**: open `~/jarvis-workspace/Knowledge` in Obsidian; ask Jarvis to remember how something is set up and
  check the librarian filed one linked note (and updates it rather than adding a second next time).

## Optional, later
- Split the repos (core, face, satellite) once the protocol has settled; v1 is frozen, which makes that possible.
- Tailscale (or another private network) plus device tokens for access beyond `ssh -L`; the core still binds to
  127.0.0.1 and every client already needs a token once anything is paired.
- Article blades (a reader view) would need a server-side sanitiser that serves pages from a separate origin.
- The watchers' own commands don't count against `max_actions_per_hour` (they're adopted, read-only and few).

## Running the tests

```
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                   # plus: websockets httpx for the tests
python -I tests/run.py                            # every test in its own process; "All tests passed."
```
Tests never call Claude, use only localhost, never touch the real `~/.jarvis` (each sets `USERPROFILE`/`HOME`/
`JARVIS_WORKSPACE` to a temp folder before importing) and never download models: `speech.STT_AVAILABLE =
speech.TTS_AVAILABLE = False` in any test that starts `jarvis.main()` or a `WebServer` with a real `Speech`.

## Things to know
- `hub.Hub` is the single `_io`; `guardrails._io` and `jarvis._io` are the hub. Channels are duck-typed (the hub's
  docstring). The phone is `phone.Channels(None, ...)`.
- Turns run one at a time through `hub.run_turns()`; `listen()` calls `hub.end_turn()` and `hub.begin_injected()`.
- A line's `source`/`trust`/`kind` are recorded when it arrives (a text must never pass for a call).
- `web.current` is the `WebServer`; `ui_tools` draws through it → `hub.display()`.
- Line endings: most root `.py` files and some tests are CRLF, the face's sources LF; keep each file's endings.
- `updater.GUARDED_FILES` are the safety files; an update touching one is flagged.
- The face builds with `cd web && npm install && npx tsc -b && npx vite build`; `web/node_modules` and `web/dist` are
  ignored. npm 11 skips dependency install scripts (esbuild, onnxruntime-node, protobufjs); the build doesn't need them.
