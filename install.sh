#!/usr/bin/env bash
# Installs Jarvis on Linux, into .venv in this folder. Safe to run again (after an update, too).
#
#   ./install.sh              install or update
#   ./install.sh --service    ... and start Jarvis at boot (a systemd user service)
#   ./install.sh --no-browser ... without downloading Chromium for Jarvis's own browser now
#
# Needs: Python 3.11+, Node.js 20+, and Claude Code logged in with a Claude subscription.
set -euo pipefail
cd "$(dirname "$0")"

SERVICE=0; BROWSER=1
for arg in "$@"; do
  case "$arg" in
    --service) SERVICE=1 ;;
    --no-browser) BROWSER=0 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
fail() { printf '\nInstall stopped: %s\n' "$*" >&2; exit 1; }

step "Python"
PYTHON=""
for p in python3.13 python3.12 python3.11 python3; do
  if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PYTHON=$(command -v "$p"); break
  fi
done
[ -n "$PYTHON" ] || fail "Python 3.11 or newer wasn't found. Debian/Ubuntu: sudo apt install python3 python3-venv"
note "using $("$PYTHON" --version) ($PYTHON)"
# A .venv without pip (a run that stopped half way) is made again.
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  rm -rf .venv
  "$PYTHON" -m venv .venv || fail "couldn't make the virtual environment. Debian/Ubuntu: sudo apt install python3-venv"
fi
VPY=.venv/bin/python

step "Python packages (a few minutes the first time)"
"$VPY" -m pip install --quiet --upgrade pip
GPU_VERSION=$(sed -n 's/^onnxruntime-gpu\[[^]]*\]==\([0-9.]*\).*/\1/p' requirements.txt)
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
  note "NVIDIA GPU found: installing the GPU speech engines"
  "$VPY" -m pip install --quiet -r requirements.txt
  # kokoro-onnx, openwakeword and faster-whisper pull in the CPU-only onnxruntime, which shares the
  # GPU build's files: take it out and put the GPU build back.
  "$VPY" -m pip uninstall --quiet -y onnxruntime || true
  "$VPY" -m pip install --quiet --force-reinstall --no-deps "onnxruntime-gpu==$GPU_VERSION"
else
  note "no NVIDIA GPU: speech runs on the CPU (skipping several GB of CUDA libraries)"
  grep -v -E '^(nvidia-|onnxruntime-gpu)' requirements.txt > .venv/requirements-cpu.txt
  echo onnxruntime >> .venv/requirements-cpu.txt
  "$VPY" -m pip install --quiet -r .venv/requirements-cpu.txt
fi

step "Node.js and the browser interface"
command -v node >/dev/null || fail "Node.js 20 or newer isn't installed (https://nodejs.org, or your package manager)"
node -e 'process.exit(+process.versions.node.split(".")[0] < 20)' || fail "Node.js is $(node --version); 20 or newer is needed"
(cd web && npm ci --no-audit --no-fund --loglevel=error && npm run build --silent) || fail "building the browser interface failed (above)"
note "built web/dist"

if [ "$BROWSER" = 1 ]; then
  step "Chromium for Jarvis's own browser"
  PW=$("$VPY" -c 'import browser_launch; print(browser_launch.PLAYWRIGHT_MCP)')
  npx -y "$PW" install-browser chromium >/dev/null 2>&1 && note "ready" ||
    note "couldn't download it now; Jarvis tries again the first time he uses his browser"
fi

step "Claude Code"
if command -v claude >/dev/null; then
  note "found $(claude --version 2>/dev/null | head -1). Log in once by running: claude"
else
  note "not installed. Install it, then log in once:"
  note "  npm install -g @anthropic-ai/claude-code && claude"
fi
[ -z "${ANTHROPIC_API_KEY:-}" ] || note "WARNING: ANTHROPIC_API_KEY is set. Jarvis refuses to start with it (it bills per token); unset it."

step "Settings folder and optional extras"
mkdir -p "$HOME/.jarvis" && chmod 700 "$HOME/.jarvis"
note "secrets, memory, rules and logs live in $HOME/.jarvis (fill in secrets.env, or use the Settings panel)"
if ! { ldconfig -p 2>/dev/null | grep -q libportaudio; }; then
  if command -v apt >/dev/null; then cmd="sudo apt install libportaudio2"
  elif command -v dnf >/dev/null; then cmd="sudo dnf install portaudio"
  elif command -v pacman >/dev/null; then cmd="sudo pacman -S portaudio"
  else cmd="install PortAudio"; fi
  note "voice at this PC (--voice) needs PortAudio: $cmd   (the browser interface doesn't)"
fi
command -v cloudflared >/dev/null || note "texts and calls need cloudflared on PATH (https://github.com/cloudflare/cloudflared)"

if [ "$SERVICE" = 1 ]; then
  step "Service"
  "$VPY" jarvisctl.py install-service
fi

step "Done"
note "start:      .venv/bin/python run_jarvis.py"
note "pair a browser (in a second terminal): .venv/bin/python jarvisctl.py pair --name desktop"
note "then open the address it prints. More in STARTUP.md."
