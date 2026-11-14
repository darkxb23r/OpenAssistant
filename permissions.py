"""
The user's own permission rules: ~/.jarvis/permissions.toml (see permissions.example.toml).

    [folders]
    trusted    = ['C:\\Users\\me\\Projects\\Beancount-Portable']  # extra free-zone folders
    always_ask = ['C:\\Users\\me\\Documents\\Finance']            # anything naming a path in here asks
    [commands]
    allow      = ["git *", "npm test"]                          # never ask (the safety floor still does)
    always_ask = ["git push *"]                                 # always ask, even if otherwise allowed

This module reads, checks, matches and writes that file. guardrails.py applies the rules (and
re-checks folders against its full protected list); jarvisctl.py edits the file, so this module
uses only the standard library. ~/.jarvis is a secret folder, so Jarvis's own tools can't touch the
file: it changes through jarvisctl, the user's editor, or a propose_rule the user said yes to.
"""
import json
import os
import re
import tomllib
from pathlib import Path

PERMISSIONS_FILE = Path.home() / ".jarvis" / "permissions.toml"
PROJECT_DIR = Path(__file__).resolve().parent

# kind -> (table, key). The kinds are what propose_rule and jarvisctl take; also the listing order.
KINDS = {
    "trust_folder": ("folders", "trusted"),
    "ask_folder": ("folders", "always_ask"),
    "allow": ("commands", "allow"),
    "always_ask": ("commands", "always_ask"),
    "claude_settings": ("folders", "claude_settings"),
}
FOLDER_KINDS = ("trust_folder", "ask_folder", "claude_settings")
LABELS = {"trust_folder": "trusted folder", "ask_folder": "always-ask folder",
          "allow": "allowed command", "always_ask": "always-ask command",
          "claude_settings": "project that follows its own Claude settings"}


def empty() -> dict:
    return {kind: [] for kind in KINDS}


def parse(text: str) -> dict:
    """{kind: [values]} from the file's text. ValueError says what's wrong (unknown keys too, so a
    typo like "alow" doesn't silently do nothing)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"not valid TOML: {e}") from e
    rules = empty()
    tables = {t for t, _ in KINDS.values()}
    for table, body in data.items():
        if table not in tables or not isinstance(body, dict):
            raise ValueError(f"unknown section [{table}] (use [folders] and [commands])")
        for key, values in body.items():
            kind = next((k for k, tk in KINDS.items() if tk == (table, key)), None)
            if kind is None:
                raise ValueError(f"unknown setting {table}.{key}")
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{table}.{key} must be a list of strings")
            for value in values:
                if problem := (folder_shape_problem(value) if kind in FOLDER_KINDS else pattern_problem(value)):
                    raise ValueError(f"{table}.{key}: {problem}")
            rules[kind] = [v.strip() for v in values]
    return rules


def load(path: Path = None) -> dict:
    """The rules in the file (none if it doesn't exist). ValueError if it can't be used."""
    path = path or PERMISSIONS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"can't read it: {e}") from e
    return parse(text)


def stamp(path: Path = None):
    """What changes when the file does (mtime and size), or None when there's no file."""
    try:
        st = (path or PERMISSIONS_FILE).stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


# --- checking ----------------------------------------------------------------------------------
def pattern_problem(pattern: str) -> str | None:
    """Why a command pattern can't be a rule. It must start with a literal program name: a rule
    like "*" or "* --help" would match every program."""
    words = pattern.split()
    if not words:
        return "give a command pattern, like git *"
    if "\n" in pattern or "\r" in pattern:
        return f"{pattern!r}: one line only"
    if "*" in words[0] or "?" in words[0]:
        return f"{pattern!r} must start with a program name, like git *"
    return None


def folder_shape_problem(path: str) -> str | None:
    if not path.strip() or not Path(path.strip()).expanduser().is_absolute():
        return f"{path!r}: give the folder's full path, like C:\\Users\\me\\Projects\\x"
    return None


def full(path: str) -> Path:
    return Path(path.strip()).expanduser().resolve()


def trusted_folder_problem(path: str, protected: list[Path], home: Path = None,
                           project: Path = None) -> str | None:
    """Why a folder can't be trusted: protected (or inside a protected folder), a drive root, the
    home folder itself, or one that holds Jarvis's own program folder."""
    if problem := folder_shape_problem(path):
        return problem
    folder, home = full(path), (home or Path.home()).resolve()
    project = (project or PROJECT_DIR).resolve()
    if folder == Path(folder.anchor) or folder.parent == folder:
        return f"{folder} is a whole drive; trust a folder inside it instead"
    if folder == home or home.is_relative_to(folder):
        return f"{folder} is or holds your home folder; trust a folder inside it instead"
    if project.is_relative_to(folder):
        return f"{folder} holds Jarvis's own program folder ({project}), which only changes through an update"
    if any(folder.is_relative_to(d) for d in protected):
        return f"{folder} is protected"
    return None


def basic_protected(home: Path = None) -> list[Path]:
    """The protected folders jarvisctl checks before writing a trusted folder. guardrails.py checks
    again with its full list (PROTECTED_DIRS) when it reads the file; test_rules keeps them in step."""
    home = home or Path.home()
    dirs = [home / d for d in (".jarvis", ".claude", ".codex", ".ssh", ".caveman", ".gnupg", ".password-store")]
    dirs += [home / ".local" / "share" / "keyrings", PROJECT_DIR]
    dirs += [Path(os.environ[v]) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)]
    if os.name == "posix":
        dirs += [Path(d) for d in ("/boot", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
                                   "/opt", "/var", "/root", "/sys", "/proc", "/dev", "/snap")]
    return [d.resolve() for d in dirs]


def rule_problem(kind: str, value: str, protected: list[Path] | None = None) -> str | None:
    """Why (kind, value) can't be added, or None."""
    if kind not in KINDS:
        return f"unknown kind {kind!r}: use one of {', '.join(KINDS)}"
    if kind in ("trust_folder", "claude_settings"):
        return trusted_folder_problem(value, basic_protected() if protected is None else protected)
    if kind == "ask_folder":
        return folder_shape_problem(value)
    return pattern_problem(value)


# --- matching ----------------------------------------------------------------------------------
# Where one command ends and the next begins: ; && || | & and new lines. Quotes aren't respected,
# which only ever splits more (every piece must then pass on its own), never less.
SEPARATORS = re.compile(r"&&|\|\||[;|&\n\r]")
# Redirections that only silence or merge output, removed before matching.
HARMLESS_REDIRECTS = re.compile(r"(?<!\S)(?:[12]?>\s*(?:/dev/null|\$null|nul)|2>&1|[12]>&[12])(?!\S)", re.IGNORECASE)
# What runs other code inside one command, or writes a file: an allow rule never covers these.
CODE_INSIDE = {"Bash": re.compile(r"`|\$\(|\$\{|[<>]"),
               "PowerShell": re.compile(r"[`(){}<>]|\$\(|@\(|@\{")}


# Changing folder only sets where the next command runs (where it points is checked before rules are).
CHANGE_FOLDER = re.compile(r"(?:cd|chdir|pushd|popd|set-location|sl)(?: .*)?", re.IGNORECASE | re.DOTALL)


def normalise(text: str) -> str:
    return " ".join(text.split())


def segments(cmd: str) -> list[str]:
    """The commands in a chained command line, each with its whitespace normalised."""
    return [s for s in (normalise(p) for p in SEPARATORS.split(HARMLESS_REDIRECTS.sub(" ", cmd))) if s]


def _regex(pattern: str, ignore_case: bool) -> re.Pattern:
    """A glob: * is anything (a trailing " *" also matches the bare command), whitespace normalised."""
    text = normalise(pattern)
    tail = text.endswith(" *")
    parts = (text[:-2] if tail else text).split("*")
    body = ".*".join(re.escape(p) for p in parts) + (r"(?: .*)?" if tail else "")
    return re.compile(body, re.IGNORECASE | re.DOTALL if ignore_case else re.DOTALL)


def matches(pattern: str, segment: str, tool_name: str) -> bool:
    return bool(_regex(pattern, tool_name == "PowerShell").fullmatch(normalise(segment)))


def first_match(patterns: list[str], segment: str, tool_name: str) -> str | None:
    return next((p for p in patterns if matches(p, segment, tool_name)), None)


def asking_rule(patterns: list[str], tool_name: str, cmd: str) -> str | None:
    """The always-ask pattern found anywhere in the command: in any chained command, and inside
    quotes too (bash -c "git push"). Finding one where it isn't really run only asks once more."""
    text = normalise(re.sub(r"[\"'`]", " ", cmd))
    for pattern in patterns:
        body = _regex(pattern, tool_name == "PowerShell")
        if re.search(rf"(?<![\w./\\-])(?:{body.pattern})(?![\w-])", text, body.flags):
            return pattern
    return None


def allowing_rules(patterns: list[str], tool_name: str, cmd: str, read_only) -> list[str] | None:
    """The allow patterns that cover the command: every chained command must match one, be
    read-only (read_only(tool_name, segment)), or only change folder, and at least one must match.
    None when they don't cover it, or when it runs other code inside itself or redirects to a file."""
    if not patterns:
        return None
    check = HARMLESS_REDIRECTS.sub(" ", cmd)
    if CODE_INSIDE.get(tool_name, CODE_INSIDE["PowerShell"]).search(check):
        return None
    used = []
    pieces = segments(cmd)
    for piece in pieces:
        hit = first_match(patterns, piece, tool_name)
        if hit:
            used.append(hit)
        elif not (CHANGE_FOLDER.fullmatch(piece) or read_only(tool_name, piece)):
            return None
    return sorted(set(used)) or None


# --- writing -----------------------------------------------------------------------------------
HEADER = """\
# Jarvis's permission rules. Changes apply within a moment; no restart needed.
# Edit by hand, or with jarvisctl: rules, allow, always-ask, trust-folder, ask-folder, remove.
# A broken file is ignored (the last good rules stay) and Jarvis says why.
"""
COMMENTS = {
    "trust_folder": "# Like your free zone: Jarvis changes, deletes and runs commands here without asking.\n"
                    "# Protected folders inside stay protected.",
    "ask_folder": "# Anything that names a path in here asks first, even just reading it.",
    "allow": "# Commands that never ask. * is anything. Every command in a chain must match (or only\n"
             "# read). Never overrides: power/disk/hidden code, secret folders, Jarvis's own folder,\n"
             "# new machines, the always-ask rules, or standing orders.",
    "always_ask": "# Commands that always ask, even when something else would allow them.",
    "claude_settings": "# Projects whose own Claude Code settings (.claude/settings.json and settings.local.json)\n"
                       "# Jarvis follows for commands he runs there (starting with cd into the project): their\n"
                       "# Bash/PowerShell allow rules run without asking, and their ask and deny rules always ask.\n"
                       "# The same safety floor still applies. Changing those files always asks you.",
}


# --- a project's own Claude Code settings ---------------------------------------------------------
CLAUDE_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
CLAUDE_RULE = re.compile(r"(Bash|PowerShell)(?:\((.*)\))?", re.DOTALL)
ALL = "*"


def claude_rules(folder: Path) -> dict:
    """{"allow"|"ask"|"deny": {"Bash"|"PowerShell": [patterns]}} from the project's Claude settings, in
    Jarvis's pattern form: Claude's "npm run test:*" (that prefix) is "npm run test *", and a bare
    "Bash" or "Bash(*)" is ALL. Other tools' rules, and files that can't be read, are left out."""
    rules = {"allow": {}, "ask": {}, "deny": {}}
    for name in CLAUDE_SETTINGS:
        try:
            perms = json.loads((folder / name).read_text(encoding="utf-8")).get("permissions") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for which in rules:
            for entry in perms.get(which) or []:
                m = CLAUDE_RULE.fullmatch(str(entry).strip())
                if not m:
                    continue
                pattern = (m[2] or "").strip()
                pattern = ALL if pattern in ("", "*") else re.sub(r":\*$", " *", pattern)
                if pattern == ALL or not pattern_problem(pattern):
                    rules[which].setdefault(m[1], []).append(pattern)
    return rules


def _toml_string(value: str) -> str:
    if "'" not in value and not any(c in value for c in "\n\r\t") and value.isprintable():
        return f"'{value}'"          # a literal string: no escapes, so Windows paths read as written
    return json.dumps(value, ensure_ascii=False)


def render(rules: dict) -> str:
    out = [HEADER]
    for table in ("folders", "commands"):
        out.append(f"\n[{table}]\n")
        for kind, (t, key) in KINDS.items():
            if t != table:
                continue
            values = rules.get(kind, [])
            out.append(COMMENTS[kind] + "\n")
            if values:
                out.append(f"{key} = [\n" + "".join(f"    {_toml_string(v)},\n" for v in values) + "]\n")
            else:
                out.append(f"{key} = []\n")
    return "".join(out)


def save(rules: dict, path: Path = None) -> None:
    path = path or PERMISSIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render(rules)
    parse(text)   # never write a file Jarvis couldn't read back
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def numbered(rules: dict) -> list[tuple[int, str, str]]:
    """[(n, kind, value)], numbered from 1 in KINDS order (what `jarvisctl rules` shows)."""
    flat = [(kind, v) for kind in KINDS for v in rules.get(kind, [])]
    return [(i, kind, v) for i, (kind, v) in enumerate(flat, 1)]


def same(kind: str, a: str, b: str) -> bool:
    if kind in FOLDER_KINDS:
        try:
            return full(a) == full(b)
        except (OSError, ValueError):
            return a.strip() == b.strip()
    return normalise(a) == normalise(b)


def add(kind: str, value: str, path: Path = None, protected: list[Path] | None = None) -> str:
    """Add a rule to the file (made if missing), keeping every other entry. Returns what happened.
    ValueError if it can't be added or the file can't be read."""
    if problem := rule_problem(kind, value, protected):
        raise ValueError(problem)
    rules = load(path)
    value = value.strip() if kind in FOLDER_KINDS else normalise(value)
    if any(same(kind, v, value) for v in rules[kind]):
        return f"{LABELS[kind]} {value} is already a rule."
    rules[kind].append(value)
    save(rules, path)
    return f"Added {LABELS[kind]} {value}."


def remove(which: str, path: Path = None) -> str:
    """Remove a rule by its number in `numbered`, or by its exact value."""
    rules = load(path)
    listed = numbered(rules)
    if which.strip().isdigit():
        hit = next(((k, v) for n, k, v in listed if n == int(which)), None)
    else:
        hit = next(((k, v) for _, k, v in listed if same(k, v, which)), None)
    if hit is None:
        raise ValueError(f"no rule {which!r}; `jarvisctl rules` lists them")
    kind, value = hit
    rules[kind] = [v for v in rules[kind] if v != value]
    save(rules, path)
    return f"Removed {LABELS[kind]} {value}."
