"""The Settings panel (settings.py, web.py): secrets.env edited from the interface. Secrets are input
only (their values never reach the page); every value is checked, one line, known keys only, so
nothing else can be slipped into the file; the file keeps its other lines and stays private; and
only a face the user is at may change anything."""
import asyncio, json, os, socket, stat, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
for key in ("JARVIS_NAME", "JARVIS_CODEX", "ELEVENLABS_API_KEY"):
    os.environ.pop(key, None)
sys.path.insert(0, str(ROOT))
import control
import mcp_launch
import settings as st
import web as w
import websockets
from hub import Hub

SECRET = "sk_live_" + "9" * 40
(home / ".jarvis").mkdir(exist_ok=True)
mcp_launch.SECRETS_FILE.write_text(
    f"# my settings\nELEVENLABS_API_KEY={SECRET}\nMAIL_PASSWORD=hunter2hunter2\nJARVIS_NAME=Friday\nOTHER_THING=kept\n",
    encoding="utf-8")

# The listing: plain settings shown, secrets only as set or not.
listed = {f["key"]: f for f in st.listing()}
assert SECRET not in json.dumps(listed) and "hunter2" not in json.dumps(listed)
assert listed["ELEVENLABS_API_KEY"]["set"] and "value" not in listed["ELEVENLABS_API_KEY"]
assert not listed["TWILIO_AUTH_TOKEN"]["set"] and listed["JARVIS_NAME"]["value"] == "Friday"
assert "JARVIS_CONTAINED" not in listed and "JARVIS_WORKSPACE" not in listed   # never from here

# Checks: nothing is written unless every value fits.
bad = {
    "JARVIS_CONTAINED": "1",                                  # not a setting
    "ELEVENLABS_VOICE_ID": "abc\nJARVIS_CONTAINED=1",          # a second line
    "MY_PHONE_NUMBER": "555-1234",                             # not +E.164
    "JARVIS_CALL_PIN": "12a4",
    "DAV_URL": "http://dav.example.com",                       # https only
    "MAIL_HOST": "mail example.com",
    "JARVIS_NAME": "{os_name}",
    "TWILIO_AUTH_TOKEN": ' "abc" ',
}
before = mcp_launch.SECRETS_FILE.read_text(encoding="utf-8")
errors = st.save(bad)["errors"]
assert set(errors) == set(bad), errors
assert mcp_launch.SECRETS_FILE.read_text(encoding="utf-8") == before

# The meaning may be up to 750 characters.
assert st.check({"JARVIS_NAME_MEANING": "m" * 750}) == {}
assert "750 characters at most" in st.check({"JARVIS_NAME_MEANING": "m" * 751})["JARVIS_NAME_MEANING"]

# A save: replaced, added and cleared in place; other lines and comments kept; private.
result = st.save({"ELEVENLABS_API_KEY": "sk_new_" + "1" * 30, "MY_PHONE_NUMBER": "+15551234567",
                  "MAIL_PASSWORD": None, "JARVIS_NAME": "", "JARVIS_CODEX": "1"})
assert result == {"saved": ["ELEVENLABS_API_KEY", "JARVIS_CODEX", "JARVIS_NAME", "MAIL_PASSWORD", "MY_PHONE_NUMBER"]}
lines = mcp_launch.SECRETS_FILE.read_text(encoding="utf-8").splitlines()
assert lines[0] == "# my settings" and "OTHER_THING=kept" in lines
values = mcp_launch.read_secrets()
assert values["ELEVENLABS_API_KEY"].startswith("sk_new_") and values["MY_PHONE_NUMBER"] == "+15551234567"
assert "MAIL_PASSWORD" not in values and "JARVIS_NAME" not in values and values["JARVIS_CODEX"] == "1"
if os.name != "nt":
    assert stat.S_IMODE(mcp_launch.SECRETS_FILE.stat().st_mode) == 0o600


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def connect(port, kind="face", origin="http://localhost:5173", token=None):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/", origin=origin) if origin else \
        await websockets.connect(f"ws://127.0.0.1:{port}/")
    await ws.send(json.dumps({"type": "hello", "protocol": 1, "client": {"kind": kind, "name": "t"},
                              "caps": {"display": kind == "face"}, **({"token": token} if token else {})}))
    assert json.loads(await ws.recv())["type"] == "welcome"
    assert json.loads(await ws.recv())["type"] == "ready"
    return ws


async def reply(ws, frame, wanted):
    await ws.send(json.dumps(frame))
    while True:
        got = json.loads(await asyncio.wait_for(ws.recv(), 3))
        if got["type"] == wanted:
            return got


async def over_the_socket():
    port = free_port()
    server = w.WebServer(Hub(), [], port=port)
    server.control = ctl = control.Control("control-token")
    restarted = []

    @ctl.command("restart")
    async def restart(args):
        restarted.append(1)
        return {"restarting": True}

    async with server:
        face = await connect(port)
        fields = (await reply(face, {"type": "settings_get"}, "settings"))["fields"]
        assert "sk_new_" not in json.dumps(fields) and "+1555" not in json.dumps(fields)
        saved = await reply(face, {"type": "settings_set", "values": {"ELEVENLABS_MODEL": "eleven_flash_v2_5"}},
                            "settings_saved")
        assert saved == {"type": "settings_saved", "saved": ["ELEVENLABS_MODEL"]}
        refused = await reply(face, {"type": "settings_set", "values": {"MAIL_HOST": "bad host"}}, "settings_saved")
        assert "MAIL_HOST" in refused["errors"]
        await face.send(json.dumps({"type": "settings_restart"}))
        for _ in range(50):
            if restarted:
                break
            await asyncio.sleep(0.02)
        assert restarted == [1]
        await face.close()
        # A terminal (even with the control token) can't: the panel is the face's.
        term = await connect(port, kind="terminal", origin=None, token="control-token")
        got = await reply(term, {"type": "settings_set", "values": {"JARVIS_NAME": "Mallory"}}, "settings")
        assert "error" in got and "fields" not in got
        await term.close()

        # Nor a satellite (anyone in the room), nor a face without the user's trust.
        class Fake:
            def __init__(self, kind, trust): self.kind, self.trust, self.name, self.sent = kind, trust, "x", []
            async def write(self, frame): self.sent.append(frame)
        for kind, trust in (("satellite", "device"), ("face", "sms")):
            fake = Fake(kind, trust)
            await server._settings(fake, "settings_get", {})
            await server._settings(fake, "settings_set", {"values": {"JARVIS_NAME": "Mallory"}})
            assert all("fields" not in f and "saved" not in f for f in fake.sent), fake.sent
    assert mcp_launch.read_secrets().get("JARVIS_NAME") is None


asyncio.run(over_the_socket())
print("ALL OK")
