"""Controlling a running core (control.py, the /control endpoint, jarvisctl.py): only with the token,
never from a browser page, and pause works even when the core is down."""
import asyncio, io, json, os, socket, sys, tempfile
from contextlib import redirect_stdout
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import control
import guardrails as g
import httpx
import jarvis as j
import jarvisctl
import web as w
from hub import Hub


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The token file: fresh each start, private to the user where the OS allows.
token = control.new_token()
assert control.read_token() == token and len(token) >= 40
if os.name == "posix":
    assert (control.TOKEN_FILE.stat().st_mode & 0o777) == 0o600
assert control.authorized(f"Bearer {token}", token) and not control.authorized("Bearer nope", token)
assert not control.authorized(None, token) and not control.authorized(f"Bearer {token}", "")
assert control.new_token() != token
token = control.read_token()


async def main():
    port = free_port()
    hub = Hub()
    g._io = j._io = hub
    j._restart = asyncio.Event()
    stop = asyncio.Event()
    server = w.WebServer(hub, [], port=port)
    server.control = control.Control(token)
    j.control_commands(server.control, hub, stop, server)
    async with hub, server:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            auth = {"Authorization": f"Bearer {token}"}
            post = lambda cmd, headers=auth, **body: c.post(f"/control/{cmd}", json=body, headers=headers)
            # Refused: no token, a wrong one, a browser page (any Origin), another host's name.
            assert (await post("status", headers={})).status_code == 403
            assert (await post("status", headers={"Authorization": "Bearer wrong"})).status_code == 403
            assert (await post("status", headers={**auth, "Origin": "http://localhost:5173"})).status_code == 403
            assert (await post("status", headers={**auth, "Host": "evil.example"})).status_code == 403
            assert (await post("nope")).status_code == 404
            # Status: what's connected, what's waiting.
            s = (await post("status")).json()
            assert s["pid"] == os.getpid() and s["channels"] == [] and s["questions"] == [] and not s["paused"]
            # Pause and resume: the kill-switch file.
            assert (await post("pause")).json() == {"paused": True} and control.paused()
            assert (await post("resume")).json() == {"paused": False} and not control.paused()
            # Standby, through the same path as saying it.
            assert (await post("standby", on=True)).json() == {"standby": True} and j.STANDBY_FILE.exists()
            assert (await post("standby", on=False)).json() == {"standby": False} and not j.STANDBY_FILE.exists()
            # Stop and restart only set the events the main loop waits on.
            assert (await post("stop")).json() == {"stopping": True} and stop.is_set()
            assert (await post("restart")).json() == {"restarting": True} and j._restart.is_set()
            assert not j.FRESH_FILE.exists()
            assert (await post("restart", new=True)).json() == {"restarting": True, "new": True}
            assert j.FRESH_FILE.exists()                       # the next start is a fresh one
            j.FRESH_FILE.unlink()

        # jarvisctl, against the running core.
        jarvisctl.PORT = port
        out = io.StringIO()
        with redirect_stdout(out):
            assert await asyncio.to_thread(jarvisctl.main, ["status"]) == 0
        assert "Jarvis is running" in out.getvalue() and "0 channel(s)" in out.getvalue()
        with redirect_stdout(io.StringIO()):
            assert await asyncio.to_thread(jarvisctl.main, ["pause"]) == 0
        assert control.paused()
        with redirect_stdout(io.StringIO()):
            assert await asyncio.to_thread(jarvisctl.main, ["resume"]) == 0
        assert not control.paused()
        # Pairing: a code for a browser (face) by default, or for a satellite with --kind.
        out = io.StringIO()
        with redirect_stdout(out):
            assert await asyncio.to_thread(jarvisctl.main, ["pair", "--name", "laptop"]) == 0
        code, pending = next(iter(server.devices._codes.items()))
        assert pending["kind"] == "face" and pending["name"] == "laptop"
        assert f"#pair={code}" in out.getvalue() and "satellite.py" not in out.getvalue()
        out = io.StringIO()
        with redirect_stdout(out):
            assert await asyncio.to_thread(jarvisctl.main, ["pair", "--name", "kitchen", "--kind", "satellite"]) == 0
        code, pending = next(iter(server.devices._codes.items()))
        assert pending["kind"] == "satellite" and pending["name"] == "kitchen"
        assert f"satellite.py --pair {code}" in out.getvalue() and "#pair=" not in out.getvalue()
        with redirect_stdout(io.StringIO()):   # no name: called "satellite"
            assert await asyncio.to_thread(jarvisctl.main, ["pair", "--kind", "satellite"]) == 0
        assert next(iter(server.devices._codes.values()))["name"] == "satellite"
        try:
            jarvisctl.main(["pair", "--kind", "terminal"])
            raise AssertionError("pair --kind terminal")
        except SystemExit as e:
            assert "face|satellite" in str(e)
        assert (await server.control.handle("pair", {"kind": "terminal"}))[0] == 400
        # The device is labelled with the kind that really paired, whatever the code was made for.
        device, _ = server.devices.pair(server.devices.new_code("tab", "satellite"), "face", "browser")
        assert device["kind"] == "face" and device["name"] == "tab"
        server.devices.reset()

    # The core is down: pause still works (the file is the switch); status says it isn't running.
    jarvisctl.PORT = free_port()
    with redirect_stdout(io.StringIO()):
        assert jarvisctl.main(["pause"]) == 0
    assert control.paused()
    err = io.StringIO()
    from contextlib import redirect_stderr
    with redirect_stderr(err):
        assert jarvisctl.main(["status"]) == 1
    assert "isn't answering" in err.getvalue()


asyncio.run(main())
print("ALL OK")
