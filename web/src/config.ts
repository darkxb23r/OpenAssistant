/**
 * JARVIS configuration.
 *
 * Everything here is read from Vite env vars (.env.local) so no secrets are
 * committed. See .env.example for the full list.
 */

/**
 * Vite inlines a blank `.env` entry as an empty string, not as undefined, so
 * `??` never falls through to the default — and .env.example ships every
 * optional key blank, which is exactly the shape that used to bite. Treat
 * whitespace-only as unset everywhere in this file.
 */
function str(raw: unknown): string | undefined {
  const value = typeof raw === 'string' ? raw.trim() : ''
  return value === '' ? undefined : value
}

/**
 * Fixed-choice options. An unrecognised value is nearly always a typo, and
 * quietly falling back to the default hides it until it costs you a take.
 */
function choice<T extends string>(
  name: string,
  raw: unknown,
  allowed: readonly T[],
  fallback: T,
): T {
  const value = str(raw)
  if (value === undefined) return fallback
  if ((allowed as readonly string[]).includes(value)) return value as T
  console.warn(
    `[jarvis] ${name}="${value}" is not one of ${allowed.join(' | ')} — using "${fallback}".`,
  )
  return fallback
}

/** Same, for the on/off options. Accepts true/false and 1/0. */
function flag(name: string, raw: unknown, fallback: boolean): boolean {
  const value = str(raw)?.toLowerCase()
  if (value === undefined) return fallback
  if (value === 'true' || value === '1') return true
  if (value === 'false' || value === '0') return false
  console.warn(`[jarvis] ${name}="${value}" is not true or false — using ${fallback}.`)
  return fallback
}

/**
 * Where the bridge lives. Derived once here rather than in each of the three
 * places that talk to it, so moving off the default port is a single edit.
 * `wss://` maps to `https://` on its own, which is why this is a prefix swap
 * rather than a hardcoded scheme.
 */
/**
 * A built page is served by Jarvis itself (web.py), so it talks back to
 * whichever host and port it came from. The Vite dev server is a different
 * origin, so in development the default is Jarvis's default port.
 */
export const BRIDGE_WS_URL =
  str(import.meta.env.VITE_BRIDGE_URL) ??
  (import.meta.env.DEV ? `ws://${location.hostname}:8790` : `ws://${location.host}`)
export const BRIDGE_HTTP_URL = BRIDGE_WS_URL.replace(/^ws/, 'http')

/**
 * Paths that are genuinely on this machine's disk, as opposed to app-relative
 * URLs that happen to start with a slash (`/vite.svg` is one of our own static
 * assets). macOS and Linux roots, and Windows drives — `C:\shots\a.png`, or
 * `/C:/shots/a.png` once `file://` is stripped from `file:///C:/shots/a.png`.
 * One test for the orbits, the blades and the sanitiser, so they can't drift.
 */
const DISK_PATH =
  /^(?:\/(?:Users|home|root|Volumes|Applications|System|Library|private|tmp|var|opt|mnt|media|srv|data)\/|\/?[A-Za-z]:[\\/])/

/**
 * The media ticket from Jarvis's welcome (lib/bridge.ts sets it). Once a device
 * is paired, /file, /img and /media want it in the URL (an <img> can't send
 * headers) and /stt and /tts want it as a bearer token. Null in open mode.
 */
let ticket: string | null = null
export function setMediaTicket(t: string | null): void {
  ticket = t
}

/** A /file, /img or /media URL (it already has a query), with the ticket. */
export function withTicket(url: string): string {
  return ticket ? `${url}&mt=${encodeURIComponent(ticket)}` : url
}

/** The headers /stt and /tts want: the ticket as a bearer token, when there is one. */
export function ticketHeaders(): Record<string, string> {
  return ticket ? { Authorization: `Bearer ${ticket}` } : {}
}

/**
 * A ticket lasts 12 hours (devices.py), and a tab can stay open longer. The
 * bridge renews it on a timer (a `ticket` frame) and registers how here, so a
 * 403 can also renew it at once: one request at a time, whoever asks.
 */
let renewer: (() => Promise<boolean>) | null = null
let renewing: Promise<boolean> | null = null
export function setTicketRenewer(fn: () => Promise<boolean>): void {
  renewer = fn
}
export function renewTicket(): Promise<boolean> {
  if (!renewer) return Promise.resolve(false)
  renewing ??= renewer().finally(() => {
    renewing = null
  })
  return renewing
}

/** The same /file, /img or /media URL with the current ticket in place of its own. */
export function retick(url: string): string {
  return withTicket(url.replace(/&mt=[^&#]*/, ''))
}

/**
 * A media URL failed to load: was it the ticket? True when loading it again
 * (through retick) is worth one try: its ticket is older than the current one,
 * or the current one was just renewed. Anything without a ticket is left alone.
 */
export async function recoverTicket(url: string): Promise<boolean> {
  const had = /[?&]mt=([^&#]*)/.exec(url)?.[1]
  if (!ticket || !had || !url.startsWith(BRIDGE_HTTP_URL)) return false
  if (decodeURIComponent(had) !== ticket) return true
  return renewTicket()
}

/** fetch() for /stt and /tts with the ticket; a 403 renews it once and tries again. */
export async function fetchWithTicket(url: string, init: RequestInit): Promise<Response> {
  const send = () =>
    fetch(url, { ...init, headers: { ...(init.headers as Record<string, string> | undefined), ...ticketHeaders() } })
  const res = await send()
  if (res.status !== 403 || !ticket || !(await renewTicket())) return res
  return send()
}

/**
 * Pictures and video in the page (blades, sanitised markup) that fail to load:
 * an error event doesn't bubble, so one capturing listener sees them all. Each
 * element gets one retry with a fresh ticket.
 */
export function watchMediaTickets(): void {
  document.addEventListener(
    'error',
    (e) => {
      const el = e.target
      if (!(el instanceof HTMLImageElement || el instanceof HTMLMediaElement || el instanceof HTMLSourceElement)) return
      const src = el.getAttribute('src') ?? ''
      if (el.dataset.reticked || !src.includes('mt=')) return
      el.dataset.reticked = '1'
      void recoverTicket(src).then((retry) => {
        if (!retry) return
        el.setAttribute('src', retick(src))
        if (el instanceof HTMLSourceElement) (el.parentElement as HTMLMediaElement | null)?.load()
      })
    },
    true,
  )
}

/** The `/file` URL for a disk path or file:// URL, or null if it isn't one. */
export function localFileUrl(src: string): string | null {
  const path = src.replace(/^file:\/\//i, '')
  return DISK_PATH.test(path) ? withTicket(`${BRIDGE_HTTP_URL}/file?path=${encodeURIComponent(path)}`) : null
}

/**
 * Speech output engine.
 *
 * false (default) — the browser's own speechSynthesis. Runs on-device, so
 *   speech starts on the next frame with no request and no download. This is
 *   the fastest option that exists and it's why it's the default.
 *
 * true — ElevenLabs. Noticeably better voice, but every sentence costs a
 *   round trip plus generation, which is the difference between a conversation
 *   and a walkie-talkie. Turn it on when you want the voice more than the pace.
 */
export const USE_ELEVENLABS = flag(
  'VITE_USE_ELEVENLABS',
  import.meta.env.VITE_USE_ELEVENLABS,
  false,
)

/**
 * Speech engine.
 *
 *   'system' — the browser's own speechSynthesis. Starts on the next frame,
 *     costs nothing, but is capped by whatever voices the OS ships; on macOS
 *     the British male option is compact Daniel.
 *
 *   'kokoro' — an 82M-parameter neural TTS running entirely in the browser via
 *     ONNX. Four proper British male voices and far better sound, nothing
 *     leaving the machine. MEASURED ON THIS MACHINE at q8/WebGPU it generates
 *     about 2.2x slower than realtime — "Yes, sir?" took 3.3 seconds and a
 *     thirteen-word sentence took nine. That is not a conversation, so it is
 *     not the default. Try `fp32` (see kokoro.ts) before enabling it; int8
 *     quantisation often silently falls back to CPU on WebGPU, which is the
 *     likely cause.
 */
export const TTS_ENGINE: 'kokoro' | 'system' = choice(
  'VITE_TTS_ENGINE',
  import.meta.env.VITE_TTS_ENGINE,
  ['kokoro', 'system'] as const,
  'system',
)

/**
 * Which Kokoro voice. All four are British male:
 *   bm_george — measured RP baritone, closest to the character
 *   bm_fable  — warmer
 *   bm_lewis  — lower
 *   bm_daniel — brighter
 */
export const KOKORO_VOICE = choice(
  'VITE_KOKORO_VOICE',
  import.meta.env.VITE_KOKORO_VOICE,
  ['bm_george', 'bm_fable', 'bm_lewis', 'bm_daniel'] as const,
  'bm_george',
)

export const env = {
  elevenKey: str(import.meta.env.VITE_ELEVENLABS_API_KEY) ?? '',
  elevenVoiceId:
    str(import.meta.env.VITE_ELEVENLABS_VOICE_ID) ?? 'JBFqnCBsd6RMkjVDRZzb',
  porcupineKey: str(import.meta.env.VITE_PICOVOICE_ACCESS_KEY) ?? '',
}

/**
 * Wake-word engine.
 *   'speech'    — zero setup, uses the browser's SpeechRecognition to listen for
 *                 "hey jarvis". Chrome/Edge only, audio goes to Google.
 *   'porcupine' — recommended. Runs offline in WASM, "Jarvis" is a built-in
 *                 keyword, far fewer false triggers. Needs a free AccessKey
 *                 from console.picovoice.ai.
 */
export const WAKE_ENGINE: 'speech' | 'porcupine' = env.porcupineKey
  ? 'porcupine'
  : 'speech'

