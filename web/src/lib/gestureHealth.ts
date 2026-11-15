// Pure helpers for gesture-tracking reliability: preflight verdicts and the
// "what is the tracker doing" state. No imports, so node's test runner can load it.

export type Failure = { code: string; message: string }

export type PreflightEnv = {
  secure: boolean
  hostname: string
  hasMediaDevices: boolean
  wasmOk: boolean
  modelOk: boolean
}

const LOCAL = new Set(['localhost', '127.0.0.1', '[::1]', '::1'])

/** First problem found, in the order the user can fix them; null when all is well. */
export function preflightFailure(env: PreflightEnv): Failure | null {
  if (!env.secure && !LOCAL.has(env.hostname)) {
    return {
      code: 'insecure',
      message:
        'Camera needs a secure page: open Jarvis over https:// or at http://localhost (this address is not secure, so the browser hides the camera).',
    }
  }
  if (!env.hasMediaDevices) {
    return { code: 'no-camera-api', message: 'This browser does not expose a camera (getUserMedia is unavailable).' }
  }
  if (!env.wasmOk) {
    return {
      code: 'no-wasm',
      message: 'Hand-tracking runtime missing (public/mediapipe). Run "npm install" then "npm run build" in web/.',
    }
  }
  if (!env.modelOk) {
    return {
      code: 'no-model',
      message:
        'Hand model could not be loaded: the local copy (public/mediapipe/hand_landmarker.task) is missing and the download failed. Check the connection and press Retry, or run "npm run build" in web/ while online.',
    }
  }
  return null
}

/** Turn a getUserMedia / start-up error into guidance the user can act on. */
export function describeCameraError(err: unknown): Failure {
  const e = err as { name?: string; message?: string } | null
  switch (e?.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return {
        code: 'denied',
        message: 'Camera access denied. Allow the camera in the browser address bar (lock icon), then press Retry.',
      }
    case 'NotFoundError':
    case 'OverconstrainedError':
      return { code: 'no-device', message: 'No camera found. Plug one in, then press Retry.' }
    case 'NotReadableError':
    case 'AbortError':
      return { code: 'busy', message: 'The camera is in use by another app or tab. Close it, then press Retry.' }
    default:
      return { code: 'error', message: `Gesture control failed to start: ${e?.message ?? String(err)}` }
  }
}

export type Detection = { kind: 'tracking' | 'searching' | 'no-hand' | 'low-light'; text: string }

/** After this long with nothing, say so; after the second, suggest light. */
export const NO_HAND_MS = 3000
export const LOW_LIGHT_MS = 8000

export function detectionState(count: number, gesture: string, emptyForMs: number): Detection {
  if (count > 0) return { kind: 'tracking', text: gesture ? `Tracking: ${gesture}` : 'Tracking' }
  if (emptyForMs >= LOW_LIGHT_MS) {
    return { kind: 'low-light', text: 'No hand detected. Check the lighting: a dim or backlit room hides hands.' }
  }
  if (emptyForMs >= NO_HAND_MS) return { kind: 'no-hand', text: 'No hand detected. Hold a hand up in view of the camera.' }
  return { kind: 'searching', text: 'Looking for a hand...' }
}
