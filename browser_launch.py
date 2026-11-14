#!/usr/bin/env python3
"""
Starts Jarvis's browser: a headless Chromium of its own, and the Playwright MCP server
(@playwright/mcp, pinned) driving it over the DevTools protocol, behind netguard.FilteringProxy.

Everything the browser opens goes through the proxy, which refuses loopback and the LAN, so a page
can't reach the router, the NAS, cloud metadata, or Jarvis's own interface on 127.0.0.1 (in open
mode a page from there could connect as a face and answer permission questions), nor the DevTools
port below. Chromium skips proxies for loopback unless told not to, hence `<-loopback>`; QUIC and
WebRTC's own UDP are off so nothing goes around the proxy. Playwright's own --blocked-origins isn't
used: its docs say it isn't a security boundary.

Chromium is started here, not by Playwright, so that the core can watch it: it listens for DevTools
on a random 127.0.0.1 port, written to DevToolsActivePort in its profile, where browser_view.py
reads it and screencasts the page to the interface ("show the browser"). Like any local DevTools
port, other programs running as this user could drive the browser through it; they could also read
its profile anyway.

Chromium is installed on first start (`install-browser`, a quick check afterwards). The browser has
its own profile (~/.jarvis/browser/profile, never the user's) and keeps files to the workspace
roots (Playwright's default).

Usage (from jarvis.py's MCP_SERVERS): python -I browser_launch.py --output-dir DIR
"""
import asyncio
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # -I leaves this folder off sys.path
import netguard  # noqa: E402

PLAYWRIGHT_MCP = "@playwright/mcp@0.0.85"   # pinned: a new version is an update to review
BROWSER_HOME = Path.home() / ".jarvis" / "browser"
PROFILE = BROWSER_HOME / "profile"
PORT_FILE = PROFILE / "DevToolsActivePort"   # Chromium writes its DevTools port here
VIEWPORT = (1280, 800)
CHROMIUM_FLAGS = [
    "--headless=new",
    "--proxy-bypass-list=<-loopback>",                              # loopback goes through the proxy too
    "--disable-quic",                                               # QUIC is UDP: it wouldn't
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",    # nor would WebRTC's UDP
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",                                    # a random port, in PORT_FILE
    "--no-first-run", "--no-default-browser-check", "--disable-sync",
    f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
]
# Settings from the environment that would undo the above (Playwright MCP reads PLAYWRIGHT_MCP_*).
UNSAFE_ENV = ("NODE_TLS_REJECT_UNAUTHORIZED", "NODE_OPTIONS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def npx() -> str | None:
    return shutil.which("npx")


def browsers_dir() -> Path:
    """Where Playwright keeps the browsers it installs."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def chromium(root: Path | None = None) -> Path | None:
    """The newest Chromium Playwright installed (not the headless shell: it has no full screencast)."""
    root = root or browsers_dir()
    found = []
    for build in ("chrome-linux64/chrome", "chrome-linux/chrome", "chrome-win64/chrome.exe", "chrome-win/chrome.exe",
                  "chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
        for path in glob.glob(str(root / "chromium-*" / build)):
            revision = re.search(r"chromium-(\d+)", path)
            found.append((int(revision.group(1)) if revision else 0, Path(path)))
    return max(found)[1] if found else None


def chromium_command(binary: Path, proxy_port: int) -> list[str]:
    return [str(binary), f"--user-data-dir={PROFILE}", f"--proxy-server=http://127.0.0.1:{proxy_port}",
            *CHROMIUM_FLAGS, "about:blank"]


def mcp_command(npx_path: str, cdp_port: int, output_dir: str) -> list[str]:
    return [npx_path, "-y", PLAYWRIGHT_MCP, "--cdp-endpoint", f"http://127.0.0.1:{cdp_port}",
            "--output-dir", output_dir]


def devtools_port(timeout_s: float = 20) -> int | None:
    """Chromium's DevTools port, once it has written it."""
    deadline = time.monotonic() + timeout_s
    while True:   # at least once: timeout_s=0 is "is it there now?"
        try:
            first = PORT_FILE.read_text(encoding="utf-8").splitlines()[0]
            if first.isdigit():
                return int(first)
        except (OSError, IndexError):
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def close_stale() -> None:
    """A Chromium left over from a launcher that was killed still holds the profile, and a new one
    would only hand its window to it and exit: ask the old one to close, over its DevTools."""
    port = devtools_port(timeout_s=0)
    if port is None:
        return
    try:
        import websockets.sync.client as ws_client
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            url = json.load(r)["webSocketDebuggerUrl"]
        with ws_client.connect(url, open_timeout=2, max_size=None) as ws:
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            time.sleep(1)
    except Exception:
        pass
    PORT_FILE.unlink(missing_ok=True)


def start_proxy() -> int:
    """Run the filtering proxy on its own thread; its port."""
    ready = threading.Event()
    port = []

    def run():
        loop = asyncio.new_event_loop()
        proxy = netguard.FilteringProxy()
        port.append(loop.run_until_complete(proxy.start()))
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, name="browser-proxy", daemon=True).start()
    ready.wait(10)
    if not port:
        raise SystemExit("browser_launch: the proxy didn't start")
    return port[0]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "--output-dir":
        print("usage: browser_launch.py --output-dir DIR", file=sys.stderr)
        return 2
    found = npx()
    if not found:
        print("browser_launch: npx (Node.js 20 or newer) isn't on PATH", file=sys.stderr)
        return 1
    output_dir = argv[1]
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in UNSAFE_ENV and not k.startswith("PLAYWRIGHT_MCP_")}
    # The browser itself, the first time (later, a quick check). Its output goes to stderr: stdout
    # is the MCP connection.
    if subprocess.call([found, "-y", PLAYWRIGHT_MCP, "install-browser", "chromium"], env=env,
                       stdin=subprocess.DEVNULL, stdout=sys.stderr):
        print("browser_launch: couldn't install Chromium", file=sys.stderr)
    binary = chromium()
    if binary is None:
        print(f"browser_launch: no Chromium under {browsers_dir()}", file=sys.stderr)
        return 1
    close_stale()
    proxy_port = start_proxy()
    browser = subprocess.Popen(chromium_command(binary, proxy_port), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Stopped by a signal (Jarvis restarting the server): close the browser on the way out.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        cdp_port = devtools_port()
        if cdp_port is None:
            print("browser_launch: Chromium didn't start", file=sys.stderr)
            return 1
        # stdin and stdout pass straight through: they're the MCP connection.
        return subprocess.call(mcp_command(found, cdp_port, output_dir), env=env)
    finally:
        browser.terminate()
        try:
            browser.wait(5)
        except subprocess.TimeoutExpired:
            browser.kill()
        PORT_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
