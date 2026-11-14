import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
ws = home / "ws"
os.environ["JARVIS_WORKSPACE"] = str(ws)
(home / ".ssh").mkdir()
(home / ".ssh" / "config").write_text("Host old\n    HostName 1.2.3.4\n")
sys.path.insert(0, str(ROOT))
import jarvis as j
import guardrails as g


async def fake_judge(t, c):
    return True


g.judged_safe = fake_judge


class IO:
    def ask(self, s, d): self.q = (s, d)
    def say(self, t): pass


g._io = IO()


async def run(tool, inp, reply="no"):
    task = asyncio.create_task(g.ask_before_risky(tool, inp, None)); await asyncio.sleep(0)
    asked = bool(g._pending_questions)
    if asked:
        g.answer_oldest_question(reply)
    return ("ask+" if asked else "") + ("allow" if type(await task).__name__ == "PermissionResultAllow" else "deny")


hook = lambda tool, inp: asyncio.run(g.guard_reads({"tool_name": tool, "tool_input": inp}, None, None))


async def main():
    # reading tools: secret folders go to "ask", everything else passes
    for tool, inp in [("Read", {"file_path": str(home / ".ssh" / "id_ed25519")}), ("Read", {"file_path": str(home / ".claude" / ".credentials.json")}),
                      ("Grep", {"pattern": "x", "path": str(home)}), ("Glob", {"pattern": str(home / ".ssh" / "*")}),
                      ("Glob", {"pattern": "../.codex/**", "path": str(ws)}), ("Read", {"file_path": "../.jarvis/approved.txt"})]:
        assert (await g.guard_reads({"tool_name": tool, "tool_input": inp}, None, None)).get("hookSpecificOutput"), (tool, inp)
        assert await run(tool, inp, "always") == "ask+allow"
    assert await run("Read", {"file_path": str(home / ".ssh" / "id_ed25519")}) == "ask+deny"  # "always" saved nothing
    for tool, inp in [("Read", {"file_path": "notes.md"}), ("Grep", {"pattern": "x"}), ("Glob", {"pattern": "**/*.py"}),
                      ("Read", {"file_path": "C:\\Windows\\System32\\drivers\\etc\\hosts"})]:
        assert await g.guard_reads({"tool_name": tool, "tool_input": inp}, None, None) == {}, (tool, inp)
        assert await run(tool, inp) == "allow"
    # secret folders are matched by where they point: a project's own .claude folder is fine
    proj = home / "Projects" / "Forge"
    g.FREE_ZONES.append(proj.resolve())
    await (windows_paths(proj) if os.name == "nt" else linux_paths(proj))
    await machines()


async def windows_paths(proj):
    assert await run("PowerShell", {"command": f"Get-Content {home}\\.ssh\\jarvis_ed25519"}) == "ask+deny"
    bash_proj = "/" + str(proj)[0].lower() + str(proj)[2:].replace("\\", "/")     # Git Bash form, /c/...
    fine = [
        f"cd {bash_proj} && python -B -c \"import json; print(json.load(open('.claude/worktrees/a/x.json')))\"",
        f"cd {proj} ; Get-Content .claude\settings.json",
        f"Get-Content {proj}\.claude\settings.json",
        f"cat {bash_proj}/.ssh/known_hosts_for_tests",
    ]
    for c in fine:
        assert not g.touches_secret(c), c
    secret = [
        "cat ~/.ssh/id_ed25519", "Get-Content $env:USERPROFILE\.claude\.credentials.json",
        f"cat /{str(home)[0].lower()}{str(home)[2:]}/.codex/auth.json".replace("\\", "/"),
        f"cd {home} && type .ssh\id_ed25519", "python -c \"print(open('.claude/x').read())\"",   # no cd: unknown
        "Get-Content $p\.ssh\id_rsa", "cat %USERPROFILE%\.jarvis\approved.txt",
    ]
    for c in secret:
        assert g.touches_secret(c), c
    assert await run("Bash", {"command": fine[0]}) == "allow"                    # judge stubbed SAFE
    assert await run("Bash", {"command": secret[0]}) == "ask+deny"
    assert await run("Bash", {"command": f"rm -rf {bash_proj}/build"}) == "allow"   # Git Bash path in the free zone
    assert g.native("/c/Users/x") == "C:/Users/x" and g.native("/etc/x") == "/etc/x"


async def linux_paths(proj):
    assert await run("Bash", {"command": f"cat {home}/.ssh/jarvis_ed25519"}) == "ask+deny"
    assert await run("Read", {"file_path": "/etc/hosts"}) == "allow"              # the OS's files can be read
    fine = [
        f"cd {proj} && python3 -B -c \"import json; print(json.load(open('.claude/worktrees/a/x.json')))\"",
        f"cat {proj}/.claude/settings.json",
        f"cat {proj}/.ssh/known_hosts_for_tests",
    ]
    for c in fine:
        assert not g.touches_secret(c), c
    secret = [
        "cat ~/.ssh/id_ed25519", "cat $HOME/.claude/.credentials.json", f"cat {home}/.codex/auth.json",
        f"cd {home} && cat .ssh/id_ed25519", "python3 -c \"print(open('.claude/x').read())\"",   # no cd: unknown
        "cat $p/.ssh/id_rsa", "cat ${HOME}/.jarvis/approved.txt", "cat ~/.gnupg/pubring.kbx",
    ]
    for c in secret:
        assert g.touches_secret(c), c
    assert await run("Bash", {"command": fine[0]}) == "allow"                    # judge stubbed SAFE
    assert await run("Bash", {"command": secret[0]}) == "ask+deny"
    assert await run("Bash", {"command": f"rm -rf {proj}/build"}) == "allow"      # in the free zone
    assert g.native("/c/Users/x") == "/c/Users/x"                                  # Git Bash paths are Windows-only


async def machines():
    # add_machine: always asks, validates first
    good = {"name": "nas", "address": "127.0.0.1"}
    assert await run(g.ADD_MACHINE, good, "always") == "ask+allow"
    assert await run(g.ADD_MACHINE, good) == "ask+deny"
    for bad in [{"name": "n a s", "address": "1.2.3.4"}, {"name": "nas", "address": "1.2.3.4; rm"},
                {"name": "nas", "address": "1.2.3.4", "user": "Root!"}, {"name": "nas", "address": "1.2.3.4", "port": 0}]:
        assert await run(g.ADD_MACHINE, bad) == "deny", bad
    r = await j.add_machine_tool.handler(good)
    assert not r.get("is_error"), r
    assert g.JARVIS_KEY.exists() and g.JARVIS_KEY.with_suffix(".pub").exists()
    config = (home / ".ssh" / "config").read_text()
    assert config.startswith("Include jarvis_config\n") and "Host old" in config
    jc = g.MACHINES_FILE.read_text()
    assert "Host nas" in jc and "HostName 127.0.0.1" in jc and "User jarvis" in jc and "BatchMode yes" in jc
    setup = (ws / "setup-nas.sh").read_text()
    assert 'from="127.0.0.1",no-agent-forwarding' in setup and "NOPASSWD" in setup and "ssh-ed25519 " in setup
    assert "host: nas" in g.approved and "host: 127.0.0.1" in g.approved
    assert (await j.add_machine_tool.handler(good))["is_error"]      # already there
    # a second machine keeps the first; ssh to it goes to the normal checks, not the new-machine question
    r = await j.add_machine_tool.handler({"name": "pi", "address": "pi.lan", "user": "admin", "port": 2222})
    assert set(g.read_machines()) == {"nas", "pi"} and g.read_machines()["pi"]["port"] == "2222"
    assert (home / ".ssh" / "config").read_text().count("Include jarvis_config") == 1
    assert await run("Bash", {"command": "ssh nas sudo docker restart plex"}) == "allow"   # judge (stubbed) says SAFE
    assert await run("Bash", {"command": "ssh nas sudo reboot"}) == "ask+deny"           # power still asks
    assert "nas: jarvis@127.0.0.1 port 22" in (await j.list_machines_tool.handler({}))["content"][0]["text"]
    # remove
    r = await j.remove_machine_tool.handler({"name": "nas"})
    assert not r.get("is_error") and set(g.read_machines()) == {"pi"}
    assert "host: nas" not in g.approved and "host: nas" not in g.APPROVED_FILE.read_text()
    assert await run("Bash", {"command": "ssh nas uptime"}) == "ask+deny"                # new machine again
    assert (await j.remove_machine_tool.handler({"name": "nas"}))["is_error"]
    opts = j.build_options(True)
    assert "PreToolUse" in opts.hooks and "list_machines" in opts.system_prompt


asyncio.run(main())
print("ALL OK")
