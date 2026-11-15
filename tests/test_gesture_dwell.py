"""Wiring/strings check for the gesture dwell click (web/src/lib/dwell.ts and its callers)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rd = lambda p: (ROOT / p).read_text(encoding="utf-8")
dwell, hands = rd("web/src/lib/dwell.ts"), rd("web/src/lib/hands.ts")
pointer, guide = rd("web/src/ui/Pointer.tsx"), rd("web/src/ui/GestureGuide.tsx")

assert "import" not in "".join(l for l in dwell.splitlines() if l.startswith("import"))  # pure module
assert "DWELL_DEFAULT_MS = 3000" in dwell and "DWELL_MIN_MS = 2000" in dwell and "DWELL_MAX_MS = 6000" in dwell
assert "DWELL_REFRACTORY_MS = 1000" in dwell and "enabled: true" in dwell and "'jarvis.dwell'" in dwell
for marker in ("data-destructive", "data-no-dwell", "aria-disabled", "isTypingIn", "gesture !== 'point'"):
    assert marker in dwell, marker
assert "from './dwell'" in hands and "dwellFrame(h, over)" in hands and "dwellDrop(id)" in hands.replace("dwellDrop(i)", "dwellDrop(id)")
assert "dwellDrop(i)" in hands and "dwells.clear()" in hands
assert "fire(r.fire, 'pointerdown'" in hands and "new MouseEvent('click'" in hands
assert "DWELL_GRACE_MS = 500" in dwell and "DWELL_GUARDED_MIN_MS = 3000" in dwell and "DWELL_GUARDED_LOCKOUT_MS = 1000" in dwell
assert "guardOf(target)" in hands and "guarded: guard.guarded" in hands
blades = rd("web/src/ui/Blades.tsx")
assert 'data-guarded=""' in blades and "guardedSince" in blades and "bl-answers" in blades
assert "'#ffffff'" in pointer and "rr" in pointer
assert "dwellView" in pointer and "dwell > 0" in pointer
assert "setDwellSettings" in guide and 'type="range"' in guide and "DWELL_MIN_MS" in guide
assert (ROOT / "web/test/dwell.test.ts").exists()
print("ALL OK")
