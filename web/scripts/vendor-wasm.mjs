// Copies MediaPipe's hand-tracking runtime into public/mediapipe, where the page loads it from,
// and downloads the hand model next to it so tracking works offline. Runs before `npm run dev`
// and `npm run build`. Hand tracking is optional: if anything here fails the interface still
// builds, and the page says exactly what is missing when G is pressed.
import { cpSync, existsSync, mkdirSync, renameSync, writeFileSync } from 'node:fs'

const from = 'node_modules/@mediapipe/tasks-vision/wasm'
const to = 'public/mediapipe'
if (existsSync(from) && !existsSync(`${to}/vision_wasm_internal.wasm`)) {
  try {
    mkdirSync(to, { recursive: true })
    cpSync(from, to, { recursive: true })
    console.log('vendored the hand-tracking runtime into public/mediapipe')
  } catch (err) {
    console.warn(`could not vendor the hand-tracking runtime: ${err.message}`)
  }
} else if (!existsSync(`${to}/vision_wasm_internal.wasm`)) {
  console.warn('hand-tracking runtime missing: run "npm install" so @mediapipe/tasks-vision is present')
}

// Same URL as MODEL_URL in src/lib/hands.ts, which stays as the runtime fallback.
const MODEL_URL =
  'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task'
const model = `${to}/hand_landmarker.task`
if (!existsSync(model)) {
  try {
    const res = await fetch(MODEL_URL, { signal: AbortSignal.timeout(60_000) })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const bytes = Buffer.from(await res.arrayBuffer())
    if (bytes.length < 1_000_000) throw new Error(`unexpectedly small (${bytes.length} bytes)`)
    mkdirSync(to, { recursive: true })
    writeFileSync(`${model}.part`, bytes)
    renameSync(`${model}.part`, model)
    console.log('downloaded the hand model into public/mediapipe')
  } catch (err) {
    console.warn(
      `could not download the hand model (${err.message}); the page will try ${MODEL_URL} at runtime. ` +
        'Re-run "npm run build" while online to vendor it.',
    )
  }
}
