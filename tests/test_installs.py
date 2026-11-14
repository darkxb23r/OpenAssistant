import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
ws = home / "ws"
os.environ["JARVIS_WORKSPACE"] = str(ws)
os.environ.pop("SCOOP", None)
(home / ".jarvis").mkdir()
apps = home / "apps"
(home / ".jarvis" / "free-zones.txt").write_text(f"{apps}")  # no trailing newline on purpose
sys.path.insert(0, str(ROOT))
import jarvis as j
import guardrails as g


async def fake_judge(t, c):
    return True  # everything that reaches the safety check passes, so "ask" means location said so


g.judged_safe = fake_judge


class IO:
    def ask(self, s, d): self.q = (s, d)
    def say(self, t): self.said = t


g._io = IO()


async def run(tool, inp, reply="no"):
    task = asyncio.create_task(g.ask_before_risky(tool, inp, None)); await asyncio.sleep(0)
    asked = bool(g._pending_questions)
    if asked:
        g.answer_oldest_question(reply)
    return ("ask+" if asked else "") + ("allow" if type(await task).__name__ == "PermissionResultAllow" else "deny")


ps = lambda c: run("PowerShell", {"command": c})


async def windows():
    venv = ws / "proj" / ".venv" / "Scripts"
    # installs that keep every file in the free zone go to the normal checks (here: allowed)
    for c in [f"{venv}\\pip.exe install requests", f"{venv}\\python.exe -m pip install -U requests",
              f"pip install requests --target {apps}\\lib", f"python -m pip install x --prefix={apps}\\py",
              f"npm install -g typescript --prefix {apps}\\node", f"winget install VideoLAN.VLC --location {apps}\\vlc",
              f"cargo install ripgrep --root {apps}\\cargo", f"{venv}\\pip.exe uninstall -y requests",
              f"npm uninstall -g typescript --prefix {apps}\\node"]:
        assert await ps(c) == "allow", c
    # everything else asks
    for c in ["pip install requests", "winget install VideoLAN.VLC", "winget uninstall VideoLAN.VLC",
              "pip install x --target C:\\Python313\\Lib", f"pip install x --target {home}\\.claude",
              "choco install vlc", "msiexec /i app.msi", "scoop install git", "npm install -g typescript",
              f"pip install x --target {apps}\\lib; Remove-Item C:\\x", f"winget install x --location {apps}\\x --scope machine | Out-Null",
              "Install-Module PSReadLine", f"pip install x --target relative\\lib"]:
        assert await ps(c) == "ask+deny", c
    # scoop is fine once its folder is in the free zone
    g.FREE_ZONES.append((home / "scoop").resolve())
    assert await ps("scoop install git") == "allow"


async def linux():
    bash = lambda c: run("Bash", {"command": c})
    venv = ws / "proj" / ".venv" / "bin"
    # installs that keep every file in the free zone go to the normal checks (here: allowed)
    for c in [f"{venv}/pip install requests", f"{venv}/python -m pip install -U requests",
              f"pip install requests --target {apps}/lib", f"python3 -m pip install x --prefix={apps}/py",
              f"npm install -g typescript --prefix {apps}/node", f"cargo install ripgrep --root {apps}/cargo",
              f"{venv}/pip uninstall -y requests", f"npm uninstall -g typescript --prefix {apps}/node"]:
        assert await bash(c) == "allow", c
    # everything else asks: system packages always, and anything that lands outside the free zone
    for c in ["pip install requests", "sudo apt install vlc", "apt-get remove vlc", "dnf install vlc",
              "pacman -S vlc", "snap install vlc", "flatpak install flathub org.videolan.VLC", "brew install vlc",
              "pip install x --target /usr/lib/python3/dist-packages", f"pip install x --target {home}/.claude",
              "npm install -g typescript", f"pip install x --target {apps}/lib; rm /x",
              "pip install x --target relative/lib", "pipx install httpie"]:
        assert await bash(c) == "ask+deny", c


async def main():
    await (windows() if os.name == "nt" else linux())

    # add_free_zone: always asks, even with trust mode and after "always"; bad paths never ask
    add = lambda p, r="no": run(g.ADD_FREE_ZONE, {"path": p}, r)
    g.set_trust(30)
    media = home / "media"
    assert await add(str(media)) == "ask+deny"
    assert await add(str(media), "always") == "ask+allow"
    assert await add(str(media)) == "ask+deny"  # "always" saved nothing
    g.set_trust(0)
    for bad in ["media", str(home / ".claude" / "x"), str(ws / "sub"), ""]:
        assert await add(bad) == "deny", bad
    # the tool itself: creates the folder, appends on its own line, takes effect at once
    r = await j.add_free_zone_tool.handler({"path": str(media)})
    assert not r.get("is_error") and media.is_dir(), r
    assert g.FREE_ZONES_FILE.read_text().splitlines() == [str(apps), str(media.resolve())]
    if os.name == "nt":
        assert await ps(f"Remove-Item {media}\\old -Recurse") == "allow"
    else:
        assert await run("Bash", {"command": f"rm -rf {media}/old"}) == "allow"
    assert (await j.add_free_zone_tool.handler({"path": str(media)}))["is_error"]
    assert g.ADD_FREE_ZONE not in g.APPROVED_FILE.read_text() if g.APPROVED_FILE.exists() else True


asyncio.run(main())
print("ALL OK")
