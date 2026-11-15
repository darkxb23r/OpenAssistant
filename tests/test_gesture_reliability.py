"""Wiring/strings check for the webcam gesture reliability work (web/src/lib/hands.ts etc.)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rd = lambda p: (ROOT / p).read_text(encoding="utf-8")
hands, health = rd("web/src/lib/hands.ts"), rd("web/src/lib/gestureHealth.ts")
guide, vendor, app = rd("web/src/ui/GestureGuide.tsx"), rd("web/scripts/vendor-wasm.mjs"), rd("web/src/App.tsx")

assert "MODEL_LOCAL = '/mediapipe/hand_landmarker.task'" in hands and "storage.googleapis.com" in hands
assert hands.index("[MODEL_LOCAL, MODEL_URL]") > 0 and "diag.delegate" in hands
assert "preflight()" in hands and "diag.failure" in hands and "detect failed" in hands
assert "hand_landmarker.task" in vendor and "could not download the hand model" in vendor
for code in ("insecure", "no-camera-api", "no-wasm", "no-model", "denied", "no-device", "busy"):
    assert f"code: '{code}'" in health, code
assert "Retry" in guide and "GestureStatus" in guide and "useState(false)" in guide  # debug off by default
assert "<GestureStatus live={gestures} />" in rd("web/src/ui/Hud.tsx")
assert "err?.message" in app.split("if (e.key === 'g'", 1)[1].split("'t'", 1)[0]
assert (ROOT / "web/test/gestureHealth.test.ts").exists()
print("ALL OK")
