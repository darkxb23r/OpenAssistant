import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
ws = home / "ws"
os.environ["JARVIS_WORKSPACE"] = str(ws)
contained = sys.argv[1] == "vm"
if contained:
    os.environ["JARVIS_CONTAINED"] = "1"
(home / ".jarvis").mkdir()
media = "D:\\media" if os.name == "nt" else "/srv/media"   # a zone outside the home folder
(home / ".jarvis" / "free-zones.txt").write_text(f"# media library\n{media}\nrelative\\ignored\n")
sys.path.insert(0, str(ROOT))
import jarvis as j
import guardrails as g

assert g.CONTAINED == contained
judge_calls = []


async def fake_judge(t, c):
    judge_calls.append(c)
    return False


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


PS = "PowerShell"
ps = lambda c, r="no": run(PS, {"command": c}, r)


async def windows_common():
    # deletes inside the free zone run without asking, anywhere else they ask (host mode)
    assert await ps(f"Remove-Item -Recurse -Force {ws}\\build") == "allow"
    assert await ps("Remove-Item 'D:\\media\\old show' -Recurse -ErrorAction SilentlyContinue") == "allow"
    assert await ps("rm D:\\media\\tmp\\*.log") == "allow"
    assert await ps("Remove-Item -Path:D:\\media\\x") == "allow"
    # never: relative paths, variables, lists, second commands, protected spots, zone roots
    for c in ["Remove-Item build", "Remove-Item $env:TEMP\\x", "Remove-Item D:\\media\\a,C:\\Windows",
              "Remove-Item D:\\media\\a; Remove-Item C:\\x", f"Remove-Item {home}\\.claude\\x",
              f"Remove-Item {home}\\.jarvis\\approved.txt",
              "Remove-Item -Path:C:\\Windows D:\\media\\x", "Get-ChildItem D:\\media | Remove-Item"]:
        assert await ps(c) == "ask+deny", c
    # protected spots ask even for reads
    assert await ps(f"Get-Content {home}\\.claude\\.credentials.json") == "ask+deny"
    assert await ps("Get-Process") == "allow"
    # always-ask stays
    assert await ps("Restart-Computer") == "ask+deny"
    # files
    assert await run("Write", {"file_path": "D:\\media\\notes.txt"}) == "allow"


async def linux_common():
    bash = lambda c: run("Bash", {"command": c})
    # deletes inside the free zone run without asking, anywhere else they ask (host mode)
    assert await bash(f"rm -rf {ws}/build") == "allow"
    assert await bash("rm -r '/srv/media/old show'") == "allow"
    assert await bash("rm /srv/media/tmp/*.log") == "allow"
    assert await bash("rm -rf -- /srv/media/x") == "allow"
    # never: relative paths, variables, lists, second commands, protected spots, zone roots
    for c in ["rm -rf build", "rm -rf $TMPDIR/x", "rm -rf /srv/media/a /etc/x", "rm /srv/media/a; rm /x",
              f"rm {home}/.claude/x", f"rm {home}/.jarvis/approved.txt", "rm /srv/media/a,/etc/x",
              "find /srv/media -name x | xargs rm", "shred -u /etc/x"]:
        assert await bash(c) == "ask+deny", c
    # protected spots ask even for reads
    assert await bash(f"cat {home}/.claude/.credentials.json") == "ask+deny"
    assert await bash("ps aux") == "allow"
    # always-ask stays
    assert await bash("reboot") == "ask+deny"
    # files
    assert await run("Write", {"file_path": "/srv/media/notes.txt"}) == "allow"


async def machines():
    # other machines: ask once, "always" hands the machine over
    n = len(judge_calls)
    assert await ps("Invoke-Command -ComputerName nas -ScriptBlock { Get-Date }") == "ask+deny"
    assert await run("Bash", {"command": "ssh -i key admin@nas docker restart plex"}, "always") == "ask+allow"
    assert "host: nas" in g.APPROVED_FILE.read_text()
    assert await run("Bash", {"command": "ssh nas docker restart sonarr"}) == "ask+deny"  # judge said RISKY
    assert len(judge_calls) == n + 1                                                       # judge ran for it
    assert await run("Bash", {"command": "ssh pi uptime"}) == "ask+deny"                    # new machine
    assert await ps("Enter-PSSession $s") == "ask+deny"                                     # unclear where
    assert await run("Bash", {"command": "scp backup.tar nas:/backups/"}) == "ask+deny"     # nas known -> judge
    assert await run("Bash", {"command": "ssh localhost uptime"}) == "ask+deny"             # local -> judge
    assert await run("Write", {"file_path": str(home / ".codex" / "auth.json")}) == "ask+deny"
    assert g.remote_hosts("docker -H ssh://me@nas ps") == {"nas"}
    assert g.remote_hosts("Get-ChildItem \\\\nas\\share") == {"nas"}
    assert g.remote_hosts("rsync -a /src /dst") == set()


async def free_zone_work():
    """Work inside an approved folder runs without asking, like accepting edits there."""
    bs = "\\"
    proj = ws / "proj"
    gb = "/" + str(proj)[0].lower() + str(proj)[2:].replace(bs, "/")          # Git Bash form
    free = [f"cd {gb} && git log --oneline | head -5; git status --short",
            f"cd {proj} ; python -B -c \"print(open('docs/a.md').read())\"",
            f"cd {gb} && python - <<'EOF'\nopen('{gb}/notes.md','w').write('x')\nEOF",
            f'cd {gb}/wt && cmd //c "mklink /J frontend{bs*2}node_modules ..{bs*2}frontend{bs*2}node_modules"',
            f"cd {gb} && npm install && npm test",
            f"cd {gb} && rm -rf build dist",
            f"Set-Location {proj}; Get-ChildItem -Path:{proj}{bs}src"]
    for c in free:
        assert g.location_check(c)[0] == "free", c
        assert await run("Bash", {"command": c}) == "allow", c
    held = {f"cd {gb} && npm install -g typescript": "ask",
            f"cd {gb} && cat ~/.ssh/id_ed25519": "ask",
            f"cd {gb} && rm -rf ../../../../../outside": "ask",
            f"cd {gb} && cd C:/Windows && del x": "ask",
            f"cd {gb} && cp a.txt C:/Users/other/a.txt": "check",
            "cd C:/Elsewhere && git status": "check",
            "python -c \"print(1)\"": "check",
            # paths that leave the folder without a drive letter: the root, the home folder, a drive's
            # root (Git Bash /c, or \ in cmd), a drive-relative path, a variable, a POSIX folder
            f"cd {gb} && rm -rf /": "ask",
            f"cd {gb} && rm -rf ~": "ask",
            f"cd {gb} && rm -rf /c": "ask",
            f"cd {gb} && del {bs}Windows{bs}x": "ask",
            f"cd {gb} && del C:x": "ask",
            f"cd {gb} && rm -rf $HOME/x": "ask",
            f"cd {gb} && cp x /etc/x": "check",
            f"cd {gb} && Remove-Item -Path:C:{bs}Windows{bs}x": "ask"}
    for c, want in held.items():
        assert g.location_check(c)[0] == want, (c, g.location_check(c))
    # a network share starts a path; doubled backslashes inside a word aren't one
    assert g.remote_hosts(f"dir {bs*2}nas{bs}share") == {"nas"}
    assert g.remote_hosts(f"net view {bs*2}nas") == {"nas"}
    assert g.remote_hosts(f'cmd //c "mklink /J frontend{bs*2}node_modules ..{bs*2}..{bs*2}frontend"') == set()
    # the logged false "new machine" questions: .git, source, tools, and n
    assert g.remote_hosts(f'cd {gb} && cmd //c "dir /b ..{bs*2}.git ..{bs*2}source ..{bs*2}tools"') == set()
    assert g.remote_hosts(f"python -c \"print('a{bs*2}n'.join(['x', 'y']))\"") == set()
    assert g.remote_hosts(f"python -c \"print('{bs*2}n'.join(['x', 'y']))\"") == set()
    assert g.location_check(f'cd {gb} && cmd //c "dir /b ..{bs*2}.git ..{bs*2}source ..{bs*2}tools"')[0] != "ask"
    # one delete chained only with commands that just look, all in the free zone
    gws = "/" + str(ws)[0].lower() + str(ws)[2:].replace(bs, "/")
    assert g.location_check(f"rm {gws}/scan_tools.py && ls {gws}")[0] == "free"
    assert await run("Bash", {"command": f"rm {gws}/scan_tools.py && ls {gws}"}) == "allow"
    assert await ps(f"Remove-Item {ws}{bs}old.txt; Get-ChildItem {ws}") == "allow"
    for c in [f"rm {gws}/a && ls C:/Windows && rm C:/x", f"rm {gws}/a && rm {gws}/b", f"rm {gws}/a && git push",
              f"rm {gws}/a && ls `whoami`", f"rm C:/x && ls {gws}"]:
        assert g.location_check(c)[0] == "ask", c


async def windows_host_only():
    await free_zone_work()
    assert await ps("Remove-Item D:\media") == "ask+deny"          # a zone's own folder
    assert await ps(f"Remove-Item {ws}") == "ask+deny"
    assert await ps("winget install vlc") == "ask+deny"
    assert await ps("Set-ItemProperty HKCU:\\Software\\X -Name a -Value 1") == "ask+deny"
    assert await run("Write", {"file_path": "C:\\Users\\other\\notes.txt"}) == "ask+deny"
    assert "D:\\media" in g.free_zone_text()


async def windows_vm_only():
    n = len(judge_calls)
    assert await ps("winget install vlc") == "ask+deny" and len(judge_calls) == n + 1  # judge decides now
    assert await ps("Remove-Item C:\\data\\old -Recurse") == "allow"                    # whole disk is free
    assert await run("Write", {"file_path": "C:\\Users\\other\\notes.txt"}) == "allow"
    assert await run("Write", {"file_path": "C:\\Windows\\System32\\x.dll"}) == "ask+deny"
    assert await ps("Remove-Item C:\\Windows\\Temp\\x") == "ask+deny"
    assert "contained VM" in g.free_zone_text()


async def linux_host_only():
    bash = lambda c: run("Bash", {"command": c})
    assert await bash("rm -r /srv/media") == "ask+deny"           # a zone's own folder
    assert await bash(f"rm -r {ws}") == "ask+deny"
    assert await bash("sudo apt install vlc") == "ask+deny"
    assert await bash("systemctl enable --now nginx") == "ask+deny"
    assert await run("Write", {"file_path": "/home/someone-else/notes.txt"}) == "ask+deny"
    assert "/srv/media" in g.free_zone_text()


async def linux_vm_only():
    bash = lambda c: run("Bash", {"command": c})
    n = len(judge_calls)
    assert await bash("sudo apt install vlc") == "ask+deny" and len(judge_calls) == n + 1   # judge decides now
    assert await bash("rm -rf /data/old") == "allow"                                         # whole disk is free
    assert await run("Write", {"file_path": "/home/someone-else/notes.txt"}) == "allow"
    assert await run("Write", {"file_path": "/usr/lib/x.so"}) == "ask+deny"
    assert await bash("rm /var/tmp/x") == "ask+deny"
    assert "contained VM" in g.free_zone_text()


async def main():
    if os.name == "nt":
        await windows_common()
        await machines()
        await (windows_vm_only() if contained else windows_host_only())
    else:
        await linux_common()
        await machines()
        await (linux_vm_only() if contained else linux_host_only())
    assert "{" not in j.build_options(True).system_prompt.split("Memory:")[0]


asyncio.run(main())
print("ALL OK", sys.argv[1])
