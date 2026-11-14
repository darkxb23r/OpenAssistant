# Third-party notices

OpenAssistant includes work from the following project, used under its license.

## adewaskar/jarvis

- Source: https://github.com/adewaskar/jarvis
- License: MIT, Copyright (c) 2026 Aditya Dewaskar. The full text is in [`web/LICENSE`](web/LICENSE).
- What came from it:
  - `web/`: the browser interface (the React and Three.js holographic face, the HUD, the blades and the hand-gesture
    controls), adapted to talk to OpenAssistant's core instead of the original Node bridge, without its direct
    (API key in the browser) mode.
  - `netguard.py`: ported from `bridge/net.mjs` (fetching remote media without reaching the local network).
  - `ui_tools.py`: ported from `bridge/ui.mjs` and `bridge/panels.mjs` (the tools that draw on the interface).

The MIT license asks that its copyright and permission notice stay with copies of the software; `web/LICENSE` is
that notice and must be kept.
