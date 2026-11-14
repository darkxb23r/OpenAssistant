#!/usr/bin/env python3
"""
Runs Jarvis's tests, each in its own Python process (they set up throwaway home folders when
they're imported). Exits non-zero if any test fails.

    python tests/run.py           # everything
    python tests/run.py zones     # only tests whose file name contains "zones"
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT_S = 300


def runs(pattern: str) -> list[list[str]]:
    found = []
    for test in sorted(HERE.glob("test_*.py")):
        if pattern in test.name:
            # The zone rules differ on a real PC and in a contained VM, so they run in both modes.
            if test.name == "test_zones.py":
                found += [[test.name, "pc"], [test.name, "vm"]]
            elif test.name == "test_codex_optional.py":   # Codex off (the default) and on
                found += [[test.name, "off"], [test.name, "on"]]
            else:
                found += [[test.name]]
    return found


def main() -> int:
    failed = []
    for args in runs(sys.argv[1] if len(sys.argv) > 1 else ""):
        label = " ".join(args)
        start = time.monotonic()
        try:
            done = subprocess.run([sys.executable, "-I", str(HERE / args[0]), *args[1:]], cwd=HERE,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=TIMEOUT_S)
            ok, output = done.returncode == 0, (done.stdout + done.stderr).strip()
        except subprocess.TimeoutExpired:
            ok, output = False, f"timed out after {TIMEOUT_S}s"
        skipped = ok and output.splitlines()[-1:] and output.splitlines()[-1].startswith("SKIP")
        print(f"{'SKIP' if skipped else 'PASS' if ok else 'FAIL'}  {label}  ({time.monotonic() - start:.1f}s)")
        if not ok:
            failed.append(label)
            print("\n".join("      " + line for line in output.splitlines()[-15:]))
    print(f"\n{'All tests passed.' if not failed else f'{len(failed)} failed: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
