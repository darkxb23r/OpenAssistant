"""The user's permission rules (~/.jarvis/permissions.toml): trusted and always-ask folders, allow and
always-ask command patterns, the safety floor they never override, reloading, jarvisctl's edits, and
propose_rule (a hard question only the user at the PC or on a paired screen can answer)."""
import asyncio, io, os, sys, tempfile
from contextlib import redirect_stdout
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
ws = home / "ws"
os.environ["JARVIS_WORKSPACE"] = str(ws)
os.environ.pop("JARVIS_CONTAINED", None)
(home / ".jarvis").mkdir()
bs = "\\"
bp = home / "Projects" / "Beancount-Portable"          # trusted
fin = home / "Documents" / "Finance"                   # always ask
ledger = bp / "ledger-private"                         # always ask, inside the trusted folder
for d in (bp, fin, ledger, home / ".local" / "share" / "keyrings"):
    d.mkdir(parents=True)
RULES = home / ".jarvis" / "permissions.toml"


def toml_list(values):
    return "[" + ", ".join(f"'{v}'" for v in values) + "]"


def write_rules(trusted=(), ask_folders=(), allow=(), always_ask=(), raw=None):
    before = RULES.stat().st_mtime_ns if RULES.exists() else 0
    RULES.write_text(raw if raw is not None else
                     f"[folders]\ntrusted = {toml_list(trusted)}\nalways_ask = {toml_list(ask_folders)}\n"
                     f"[commands]\nallow = {toml_list(allow)}\nalways_ask = {toml_list(always_ask)}\n",
                     encoding="utf-8")
    os.utime(RULES, ns=(before + 10**9, before + 10**9))   # a new mtime even on a coarse clock


drive_root = Path(str(home)).anchor
write_rules(trusted=[bp, home, drive_root, home / ".ssh", ROOT.parent, home / ".local"],
            ask_folders=[fin, ledger],
            allow=["git *", "npm test", "wsl -d Ubuntu *", "Restart-Computer *", "ssh *", "cat *"],
            always_ask=["git push *", "Stop-Process *", "docker logs *"])
sys.path.insert(0, str(ROOT))
import guardrails as g
import jarvis as j
import jarvisctl
import permissions

judge_calls = []


async def fake_judge(t, c):
    judge_calls.append(c)
    return False


g.judged_safe = fake_judge


class IO:
    def __init__(self): self.notices = []
    def ask(self, s, d): self.q = (s, d)
    def say(self, t): self.said = t
    def notify(self, t): self.notices.append(t)


g._io = IO()


async def run(tool, inp, reply="no", by=None, kind=None):
    task = asyncio.create_task(g.ask_before_risky(tool, inp, None)); await asyncio.sleep(0)
    asked = bool(g._pending_questions)
    if asked:
        assert g.answer_oldest_question(reply, by=by, kind=kind)
    return ("ask+" if asked else "") + ("allow" if type(await task).__name__ == "PermissionResultAllow" else "deny")


bash = lambda c, r="no": run("Bash", {"command": c}, r)
ps = lambda c, r="no": run("PowerShell", {"command": c}, r)
gb = lambda p: "/" + str(p)[0].lower() + str(p)[2:].replace(bs, "/")      # Git Bash form


def last_question():
    return g._pending_questions[0] if g._pending_questions else None


async def loading():
    # Refused trusted folders, each with one notice: the home folder, a drive root, a protected
    # folder, one holding Jarvis's program folder. The rest of the file still applies.
    assert g.TRUSTED_FOLDERS == [bp.resolve(), (home / ".local").resolve()], g.TRUSTED_FOLDERS
    g.refresh_rules()   # the notices from the first read wait for _io
    notes = g._io.notices
    assert len(notes) == 4, notes
    assert any("home folder" in n for n in notes) and any("whole drive" in n for n in notes)
    assert any("protected" in n for n in notes) and any("program folder" in n for n in notes)
    assert g.ASK_FOLDERS == [fin.resolve(), ledger.resolve()]
    assert str(bp.resolve()) in g.free_zone_text()
    example = permissions.parse((ROOT / "permissions.example.toml").read_text(encoding="utf-8"))
    assert example["allow"] == ["git *", "npm test", "wsl -d Ubuntu *"] and example["trust_folder"]
    listed = (await j.list_free_zones_tool.handler({}))["content"][0]["text"]
    assert f"{bp.resolve()} (trusted" in listed, listed
    # jarvisctl's own check of protected folders covers everything guardrails protects
    assert set(g.SECRET_DIRS) | {g.PROJECT_DIR} <= set(permissions.basic_protected())


async def allow_rules():
    n = len(judge_calls)
    assert await bash("git commit -m 'tidy up'") == "allow"
    assert await bash("git   fetch    origin") == "allow"                 # whitespace normalised
    assert await bash("git log 2>&1 | head -5") == "allow"                # chained with a read-only command
    assert await bash("npm test") == "allow"
    assert await bash("cd C:/Elsewhere/app && npm test") == "allow"       # a cd only says where it runs
    assert await ps("WSL -d ubuntu ls /home") == "allow"                  # PowerShell: any case
    assert len(judge_calls) == n                                         # none of those needed the judge
    assert await bash("cd C:/Elsewhere/app") == "ask+deny"                # ... and allows nothing by itself
    assert await bash("wsl -d ubuntu ls /home") == "ask+deny"            # Bash: case matters
    assert await bash("npm test --watch") == "ask+deny"                  # no * in that rule
    # chaining can't smuggle anything past an allow rule
    for c in ["git log; rm -rf C:/x", "git log && rm -rf C:/x", "git log || rm -rf C:/x", "git log | rm x",
              "git log\nrm -rf C:/x", "git log & del C:\\x", "git log $(rm -rf C:/x)", "git log `rm -rf C:/x`",
              "git log > C:/Windows/x", "git log; python evil.py"]:
        assert await bash(c) == "ask+deny", c
    assert await ps("git log (Remove-Item C:\\x)") == "ask+deny"
    assert await ps("git log; Remove-Item C:\\x") == "ask+deny"


async def floor():
    # allow rules never override the safety floor
    assert await bash(f"cat {gb(home)}/.ssh/id_ed25519") == "ask+deny"        # a secret folder ("cat *")
    assert await bash(f"git -C {gb(home)}/.claude log") == "ask+deny"
    assert await bash(f"git -C {ROOT} reset --hard") == "ask+deny"             # Jarvis's own folder
    assert await ps("Restart-Computer -Force") == "ask+deny"                    # always asks ("Restart-Computer *")
    assert await bash("ssh pi uptime") == "ask+deny"                            # a new machine ("ssh *")
    assert g.location_check("ssh pi uptime") == ("ask", ["pi"])


async def always_ask_rules():
    q = []
    for c, t in [("git push origin main", "Bash"), ("git push", "Bash"), ("Stop-Process -Name notepad", "PowerShell"),
                 ("get-process x | stop-process", "PowerShell"), ("docker logs plex", "Bash"),
                 ("git status && git push", "Bash"), ("bash -c \"git push --force\"", "Bash")]:
        task = asyncio.create_task(g.ask_before_risky(t, {"command": c}, None)); await asyncio.sleep(0)
        question = last_question()
        assert question and question.hard, c
        q.append(question)
        assert g.answer_oldest_question("always")      # "always" saves nothing
        assert type(await task).__name__ == "PermissionResultAllow"
    assert not g.APPROVED_FILE.exists() or "push" not in g.APPROVED_FILE.read_text()
    assert await bash("git push origin main") == "ask+deny"                     # still asks
    assert g.is_read_only("Bash", "docker logs plex")                           # read-only, yet it asked
    # guard_shell sends them through the checks even when Claude Code calls them read-only
    hook = await g.guard_shell({"tool_name": "Bash", "tool_input": {"command": "docker logs plex"}}, None, None)
    assert hook["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert await g.guard_shell({"tool_name": "Bash", "tool_input": {"command": "docker ps"}}, None, None) == {}
    assert await bash("git pushup") == "allow"                                  # a different word: "git *"


async def ask_folders():
    f = str(fin)
    # reading tools, through the hook and through decide
    for tool, inp in [("Read", {"file_path": f"{f}{bs}2026.csv"}), ("Glob", {"pattern": "*.csv", "path": f}),
                      ("Grep", {"pattern": "rent", "path": str(home / "Documents")}),     # holds Finance
                      ("Glob", {"pattern": fin.as_posix() + "/**/*.csv"})]:
        hook = await g.guard_reads({"tool_name": tool, "tool_input": inp}, None, None)
        assert hook and hook["hookSpecificOutput"]["permissionDecision"] == "ask", (tool, inp)
        assert await run(tool, inp, "always") == "ask+allow"
    assert await g.guard_reads({"tool_name": "Read", "tool_input": {"file_path": str(home / "Documents" / "x.txt")}},
                               None, None) == {}
    assert await g.guard_reads({"tool_name": "Read", "tool_input": {"file_path": f"{f}2{bs}x.txt"}}, None, None) == {}
    # file tools, even inside a trusted folder; "always" saved nothing, so it asks again
    assert await run("Write", {"file_path": f"{f}{bs}notes.txt"}, "always") == "ask+allow"
    assert await run("Write", {"file_path": f"{f}{bs}notes.txt"}) == "ask+deny"
    assert await run("Edit", {"file_path": str(ledger / "main.beancount")}) == "ask+deny"
    # shell commands naming a path inside, written any way, even read-only ones
    for c in [f"cat {gb(fin)}/2026.csv", f"cat ~/Documents/Finance/2026.csv", f"cat {f}{bs}2026.csv",
              f"cat {f.replace(bs, bs * 2)}{bs * 2}2026.csv", f"cd {gb(home)}/Documents && cat Finance/2026.csv",
              f"cd {gb(home)}/Documents/Other && cat ../Finance/2026.csv", f"git --git-dir={f}{bs}.git log",
              f"ls {gb(fin)}", f"cd {gb(bp)} && cat ledger-private/main.beancount"]:
        assert await bash(c, "always") == "ask+allow", c
        assert g.rule_asks("Bash", c), c
    assert await ps(f"Get-Content '{f}{bs}2026.csv'") == "ask+deny"
    assert not g.rule_asks("Bash", f"cat {f}2{bs}x.txt") and not g.rule_asks("Bash", f"cat {gb(home)}/Documents/x")


async def trusted_folders():
    n = len(judge_calls)
    assert await run("Write", {"file_path": str(bp / "main.beancount")}) == "allow"
    assert await run("Edit", {"file_path": str(bp / "importers" / "bank.py")}) == "allow"
    # the logged shape: cd into the folder with ;, several commands, nothing outside
    for c in [f"cd {gb(bp)}; python -c \"import beancount; print(beancount.__version__)\"; ls",
              f"cd {gb(bp)}; bean-check main.beancount && bean-report main.beancount balances | head -20",
              f"cd {gb(bp)} && python tools/import.py --out {gb(bp)}/out.beancount",
              f"cd {gb(bp)} && npm install && npm run build",
              f"rm -rf {gb(bp)}/build"]:
        assert g.location_check(c)[0] == "free", c
        assert await bash(c) == "allow", c
    assert await ps(f"Set-Location {bp}; Remove-Item -Recurse {bp}{bs}dist") == "allow"
    assert len(judge_calls) == n
    # still: leaving the folder, a second cd, a global install, a secret, the folder itself
    for c in [f"cd {gb(bp)} && rm -rf ../../x", f"cd {gb(bp)} && cd C:/Windows && del x",
              f"cd {gb(bp)} && npm install -g typescript", f"cd {gb(bp)} && cat ~/.ssh/id_ed25519",
              f"rm -rf {gb(bp)}"]:
        assert g.location_check(c)[0] == "ask", c
    # protected folders inside a trusted one stay protected
    assert g.in_free_zone(str(home / ".local" / "share" / "x"))
    assert await run("Write", {"file_path": str(home / ".local" / "share" / "keyrings" / "login.keyring")}) == "ask+deny"
    # Working in a trusted folder is trusted like trust mode: whatever runs there, even reaching outside...
    n = len(judge_calls)
    for c in [f"cd {gb(bp)} && python build.py --out C:/Elsewhere/out", f"cd {gb(bp)}; $env:X = 1; ./run.ps1",
              f"cd {gb(bp)} && curl -s http://127.0.0.1:8000/health && docker compose up -d"]:
        assert await bash(c) == "allow", c
    assert len(judge_calls) == n                                       # no safety check needed
    # ... but not without the cd into it, not after leaving it, and never past what trust mode asks about
    for c in [f"python {gb(bp)}/build.py --out C:/Elsewhere/out",       # not run in the folder
              f"cd {gb(bp)}; cd C:/Elsewhere && make",                    # leaves it
              f"cd {gb(bp)} && sudo make install",                        # machine-wide
              f"cd {gb(bp)} && rm -rf C:/Elsewhere/stuff",                # a delete outside
              f"cd {gb(bp)} && cat ~/.ssh/id_ed25519",                    # a secret
              f"cd {gb(bp)} && shutdown /s"]:                             # power
        assert await bash(c) == "ask+deny", c


async def standing_orders():
    class Run:
        order, ask_timeout_s = "backup", 5
        def quiet(self): return False
        def spend_action(self, what): return True
    token = g.agency_run.set(Run())
    try:
        assert await bash("git commit -m x") == "ask+deny"               # allow rules don't apply there
        assert await run("Write", {"file_path": str(bp / "x.txt")}) == "allow"   # trusted folders do
        assert await bash("git push") == "ask+deny"
    finally:
        g.agency_run.reset(token)


async def reloading():
    assert await bash("docker restart plex") == "ask+deny"
    write_rules(allow=["docker restart *"])
    assert await bash("docker restart plex") == "allow"
    assert await bash("git commit -m x") == "ask+deny"                    # the old rules are gone
    assert g.TRUSTED_FOLDERS == [] and g.ASK_FOLDERS == []
    g._io.notices.clear()
    for raw in ["[commands]\nalow = ['git *']\n", "[commands\nallow = 1", "[commands]\nallow = 'git *'\n",
                "[commands]\nallow = ['*']\n", "[folders]\ntrusted = ['relative/x']\n"]:
        write_rules(raw=raw)
        assert await bash("docker restart plex") == "allow", raw          # the last good rules stay
        assert await bash("docker restart sonarr") == "allow", raw
        assert len(g._io.notices) == 1 and "keeping the rules" in g._io.notices[0], (raw, g._io.notices)
        g._io.notices.clear()
    RULES.unlink()
    assert await bash("docker restart plex") == "ask+deny"                # no file: no rules


async def proposing():
    assert g.approval_key(g.PROPOSE_RULE, {"kind": "allow", "value": "x"}) is None
    # checked before asking: a bad kind, a refused folder or pattern never becomes a question
    for kind, value in [("sometimes", "git *"), ("trust_folder", str(home)), ("trust_folder", drive_root),
                        ("trust_folder", str(home / ".ssh")), ("trust_folder", str(ROOT)), ("allow", "*"),
                        ("allow", ""), ("ask_folder", "relative")]:
        assert await run(g.PROPOSE_RULE, {"kind": kind, "value": value, "reason": "r"}) == "deny", (kind, value)
    args = {"kind": "allow", "value": "docker restart *", "reason": "you said to stop asking about restarts"}
    task = asyncio.create_task(g.ask_before_risky(g.PROPOSE_RULE, args, None)); await asyncio.sleep(0)
    question = last_question()
    assert question.hard and "never ask before running docker restart *" in question.summary
    assert question.only == {"physical", "device"} and question.not_kinds == {"satellite"}
    g.set_trust(30)                                                       # trust mode doesn't answer it
    assert last_question() is question
    for by, kind in [("sms", "sms"), ("call", "phone"), ("device", "satellite"), (None, None)]:
        assert not g.answer_oldest_question("yes", by=by, kind=kind), (by, kind)
    assert g.answer_oldest_question("always", by="physical", kind="local")
    assert type(await task).__name__ == "PermissionResultAllow"
    g.set_trust(0)
    assert not g.APPROVED_FILE.exists() or "propose_rule" not in g.APPROVED_FILE.read_text()
    # the yes lets the tool write the rule, and it applies at once
    assert not RULES.exists()
    got = await j.propose_rule_tool.handler(args)
    assert not got.get("is_error") and "Added" in got["content"][0]["text"]
    assert permissions.load(RULES)["allow"] == ["docker restart *"]
    assert await bash("docker restart plex") == "allow"
    assert (await j.propose_rule_tool.handler({"kind": "trust_folder", "value": str(home)}))["is_error"]
    # only the main conversation has it; workers don't
    assert "rules" in j.MCP_SERVERS and "rules" not in j.WORKER_SERVERS
    assert g.PROPOSE_RULE not in g.AUTO_APPROVED


def jarvisctl_edits():
    RULES.write_text("# mine\n[commands]\nallow = ['make *']\n", encoding="utf-8")

    def ctl(*argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = jarvisctl.main(list(argv))
        return code, out.getvalue()

    assert ctl("allow", "git", "*")[0] == 0
    assert ctl("always-ask", "git push *")[0] == 0
    assert ctl("trust-folder", str(bp))[0] == 0
    assert ctl("ask-folder", str(fin))[0] == 0
    assert "already" in ctl("allow", "git  *")[1]
    rules = permissions.load(RULES)
    assert rules == {"trust_folder": [str(bp)], "ask_folder": [str(fin)], "allow": ["make *", "git *"],
                     "always_ask": ["git push *"], "claude_settings": []}, rules
    text = RULES.read_text(encoding="utf-8")
    assert text.startswith("# Jarvis's permission rules") and "[folders]" in text and f"'{bp}'" in text
    code, listing = ctl("rules")
    assert code == 0 and "1. trusted folder" in listing and "4. allowed command" in listing and "git push *" in listing
    for bad in [("trust-folder", str(home)), ("trust-folder", drive_root), ("trust-folder", str(home / ".claude")),
                ("allow", "*"), ("remove", "99"), ("allow",)]:
        try:
            ctl(*bad)
        except SystemExit:
            continue
        raise AssertionError(bad)
    assert permissions.load(RULES) == rules                               # nothing changed by those
    assert ctl("remove", "3")[0] == 0                                     # "make *"
    assert ctl("remove", "git push *")[0] == 0
    assert ctl("remove", str(fin) + bs)[0] == 0                           # a folder, written another way
    assert permissions.load(RULES) == {"trust_folder": [str(bp)], "ask_folder": [], "allow": ["git *"],
                                       "always_ask": [], "claude_settings": []}
    # a missing file is made
    RULES.unlink()
    assert "No permission rules" in ctl("rules")[1]
    assert ctl("allow", "npm test")[0] == 0 and permissions.load(RULES)["allow"] == ["npm test"]
    # the core picks the change up
    g.refresh_rules()
    assert g.user_rules["allow"] == ["npm test"]
    # a value TOML would choke on in a literal string still round-trips
    assert ctl("allow", "echo it's")[0] == 0 and "echo it's" in permissions.load(RULES)["allow"]


def prompt():
    text = j.build_options(True).system_prompt
    assert "propose_rule" in text and "stop asking about X" in text and "cd <the folder's full path> &&" in text
    assert "cd <the folder's full path> &&" in j.WORKER_PROMPT and "cd <the folder's full path> &&" in j.ORDER_WORKER_PROMPT


async def claude_settings():
    """Projects that follow their own Claude Code settings: allow rules there run without asking (the
    floor and location checks still first), ask/deny rules always ask, and the files can't be changed
    by Jarvis without a hard question."""
    import json
    rf, p2 = home / "Documents" / "RF", home / "Documents" / "P2"
    for d, perms in ((rf, {"allow": ["Bash", "Read", "Edit"]}),
                     (p2, {"allow": ["Bash(npm run test:*)", "PowerShell(git *)"], "ask": ["Bash(git push:*)"],
                           "deny": ["Bash(curl *)"]})):
        (d / ".claude").mkdir(parents=True)
        (d / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": perms}), encoding="utf-8")
    assert permissions.claude_rules(p2)["allow"] == {"Bash": ["npm run test *"], "PowerShell": ["git *"]}
    assert permissions.claude_rules(rf)["allow"]["Bash"] == [permissions.ALL]
    out = "C:/Elsewhere/out"
    assert await bash(f"cd {rf.as_posix()} && python build.py {out}") == "ask+deny"    # not followed yet
    write_rules(raw=f"[folders]\nclaude_settings = {toml_list([rf, p2, home])}\n")
    g.refresh_rules()
    assert g.CLAUDE_SETTINGS_FOLDERS == [rf.resolve(), p2.resolve()]                   # the home folder is refused
    assert any("not following" in n for n in g._io.notices)
    n = len(judge_calls)
    assert await bash(f"cd {rf.as_posix()} && python build.py {out}") == "allow"
    assert await bash(f"cd {gb(rf)}/src; make all {out}") == "allow"                   # a subfolder, Git Bash form
    assert len(judge_calls) == n
    for c in [f"python {rf.as_posix()}/build.py {out}",                 # no cd into it: not "run there"
              f"cd {rf.as_posix()} && cd C:/ && python x.py",           # leaves it
              f"cd {rf.as_posix()} && sudo apt install x",              # a machine-wide change still asks
              f"cd {rf.as_posix()} && Restart-Computer",                # the floor
              f"cd {rf.as_posix()} && cat {home.as_posix()}/.ssh/id_rsa"]:
        assert await bash(c) == "ask+deny", c
    hard = asyncio.create_task(g.ask_before_risky("Bash", {"command": f"cd {rf.as_posix()} && echo x > .claude/settings.local.json"}, None))
    await asyncio.sleep(0)
    assert last_question().hard
    g.answer_oldest_question("no"); await hard
    assert await bash(f"cd {rf.as_posix()} && cat .claude/settings.local.json") == "allow"   # reading is fine
    write = asyncio.create_task(g.ask_before_risky("Write", {"file_path": str(rf / ".claude" / "settings.json"), "content": "{}"}, None))
    await asyncio.sleep(0)
    assert last_question().hard and "Claude settings" in last_question().summary
    g.answer_oldest_question("always"); await write
    assert not any("settings.json" in r for r in g.approved)                            # "always" saved nothing
    p = p2.as_posix()
    assert await bash(f"cd {p} && npm run test --watch {out}") == "allow"
    assert await bash(f"cd {p} && npm run test") == "allow"
    assert await ps(f"cd {p}; git status -sb {out}") == "allow"
    assert await bash(f"cd {p} && npm run build {out}") == "ask+deny"
    for c in (f"cd {p} && git push origin main", f"cd {p} && curl http://example.com"):
        task = asyncio.create_task(g.ask_before_risky("Bash", {"command": c}, None)); await asyncio.sleep(0)
        assert last_question().hard, c
        g.answer_oldest_question("no"); await task


async def main():
    await loading()
    await allow_rules()
    await floor()
    await always_ask_rules()
    await ask_folders()
    await trusted_folders()
    await standing_orders()
    await reloading()
    await proposing()
    jarvisctl_edits()
    prompt()
    await claude_settings()
    assert not g._pending_questions


if os.name != "nt":
    print("SKIP: written for Windows paths")
    sys.exit(0)
asyncio.run(main())
print("ALL OK")
