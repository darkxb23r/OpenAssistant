// Copies MediaPipe's hand-tracking runtime into public/mediapipe, where the page loads it from.
// Runs before `npm run dev` and `npm run build`. Hand tracking is optional: without the package,
// or if the copy fails, the interface still works.
import { cpSync, existsSync, mkdirSync } from 'node:fs'

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
}
