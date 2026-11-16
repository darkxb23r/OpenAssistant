import type { Blade, Panel } from '../store'
import { BRIDGE_WS_URL, renewTicket, setMediaTicket, setTicketRenewer, watchMediaTickets } from '../config'
import { caps } from './capabilities'

export type AskHandlers = {
  /** Fires for each chunk of the spoken answer. */
  onText: (delta: string) => void
  /** Fires when Jarvis starts running a tool. */
  onTool: (name: string) => void
}

/**
 * Client for Jarvis's socket (web.py), speaking the client protocol
 * (protocol.py): a `hello` first, with this browser's token or a pairing code,
 * then turns, questions and everything Jarvis draws.
 *
 * The socket is not the session: Jarvis (web.py) keeps one conversation for as
 * long as it runs, so a dropped socket loses only what was said while it was
 * down. `watchConnection` still lets the HUD say when that happens.
 */

/** Anything the bridge sends. Deliberately loose — a frame from a future
 *  bridge build should be ignored, not crash the turn. */
type Frame = {
  type?: string
  /** The `tasks` frame: what he's working on, by name. */
  items?: unknown[]
  delta?: string
  name?: string
  text?: string
  message?: string
  panel?: Panel
  blade?: Blade
  op?: string
  args?: unknown
  id?: string
  ask?: string
  reason?: string
  mode?: string
  seconds?: number
  when?: string
  servers?: Array<string | { name?: string }>
  token?: string
  media_ticket?: string
  decision?: string
  on?: boolean
  question?: Question
  state?: { questions?: Question[]; assistant?: unknown }
}

/** Every question gets an id so its answer can be told from anyone else's. */
let askSeq = 0

let socket: WebSocket | null = null
let connecting: Promise<WebSocket> | null = null

/** Server names reported by the bridge, for the HUD readout. */
let servers: string[] = []
export const bridgeServers = () => servers

/** The list arrives twice — once from config, once with live status — so the
 *  HUD subscribes rather than reading it a single time at boot. */
let onServers: ((s: string[]) => void) | null = null
export function watchServers(fn: (s: string[]) => void) {
  onServers = fn
}

/** What he's working on (tasks and research, by name), sent whenever it changes. */
let onTasks: ((t: string[]) => void) | null = null
export function watchTasks(fn: (t: string[]) => void) {
  onTasks = fn
}

/** Panels arrive out of band — they're pushed while a turn is in flight,
 *  not returned by it. */
let onPanel: ((panel: Panel) => void) | null = null
export function watchPanels(fn: (panel: Panel) => void) {
  onPanel = fn
}

/**
 * The one request the bridge makes of us rather than the other way round.
 *
 * Everything else on this socket is pushed at the browser and needs no answer.
 * A camera frame has to travel back, so this handler is registered by the app
 * and its result is returned against the request's id.
 */
export type CaptureRequest = {
  /** 'look' for a single frame, 'watch' for a grid over time. */
  mode: 'look' | 'watch'
  reason: string
  seconds: number
  /** 'now' records forward; 'past' reads the rolling buffer. */
  when: 'now' | 'past'
}
export type CaptureResult = { data?: string; mimeType?: string; error?: string }

let onCapture: ((req: CaptureRequest) => Promise<CaptureResult>) | null = null
export function watchCapture(fn: (req: CaptureRequest) => Promise<CaptureResult>) {
  onCapture = fn
}

/** Blades arrive the same way panels do — pushed mid-turn, so the article is
 *  already open as he starts the sentence about it. */
let onBlade: ((blade: Blade) => void) | null = null
export function watchBlades(fn: (blade: Blade) => void) {
  onBlade = fn
}

/** What he's called, from each welcome (identity.py). */
let onIdentity: ((raw: unknown) => void) | null = null
export function watchIdentity(fn: (raw: unknown) => void) {
  onIdentity = fn
}

/** Jarvis taking a blade down (the browser's live view, when it's hidden). */
let onBladeClose: ((id: string) => void) | null = null
export function watchBladeClose(fn: (id: string) => void) {
  onBladeClose = fn
}

/** The server's answer to the browser-control toggle (web.py): on, or off because it was refused
 *  (an untrusted screen, no browser blade open) or taken away. */
let onBrowserControl: ((on: boolean) => void) | null = null
export function watchBrowserControl(fn: ((on: boolean) => void) | null) {
  onBrowserControl = fn
}

/** Speech nobody asked for: task news, the result of background work, a
 *  permission question. Jarvis talks between turns, so these arrive outside
 *  any ask() and are spoken on their own. */
let onSay: ((text: string) => void) | null = null
export function watchSay(fn: (text: string) => void) {
  onSay = fn
}

/** Jarvis went on standby: the page goes back to waiting for its name once
 *  he has finished saying so, instead of listening for a follow-up. */
let onStandby: (() => void) | null = null
export function watchStandby(fn: () => void) {
  onStandby = fn
}

/** Commands that redress the interface — theme, reactor, orbits, effects. Same
 *  out-of-band route as panels: JARVIS issues them while he is still mid-answer
 *  so the change is on screen as he says it, which means they cannot ride back
 *  on the turn's result. The op/args pair stays untyped here on purpose — this
 *  module is a transport, and the store is where the shape is decided. */
let onUi: ((op: string, args: any) => void) | null = null
export function watchUi(fn: (op: string, args: any) => void) {
  onUi = fn
}

/**
 * Connection state, for the UI.
 *
 *   'open'        — first connection of the page.
 *   'lost'        — the socket died. Jarvis carries on; anything he says
 *                   until we're back is not heard here. Redialled forever.
 *   'reconnected' — we're back, in the same conversation.
 *   'refused'     — Jarvis turned this page away (not paired, revoked, or a
 *                   page from another version). Not redialled; the second
 *                   argument says what to do about it.
 */
export type ConnectionState = 'open' | 'lost' | 'reconnected' | 'refused'
let onConnection: ((state: ConnectionState, message?: string) => void) | null = null
export function watchConnection(fn: (state: ConnectionState, message?: string) => void) {
  onConnection = fn
}

export function isConnected(): boolean {
  return socket?.readyState === WebSocket.OPEN
}

// ---------------------------------------------------------------------------
// Who this page is (protocol.py)
// ---------------------------------------------------------------------------

const PROTOCOL_VERSION = 1

/** Close codes Jarvis refuses a client with. None of them is worth a redial. */
const REFUSALS: Record<number, string> = {
  4001: 'This browser is no longer paired with Jarvis (it was revoked). Run `jarvisctl pair` and open the URL it prints.',
  4002: 'This page is from another version of Jarvis. Reload it, or rebuild the interface (cd web && npm run build).',
  4003: "This browser isn't paired with Jarvis. Run `jarvisctl pair` and open the URL it prints.",
}

const TOKEN_KEY = 'jarvisToken'

function storedToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

function storeToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* storage blocked: paired for the life of this page only */
  }
}

/**
 * A one-time pairing code from `jarvisctl pair`, which prints a URL ending in
 * `#pair=CODE`. The fragment never reaches a server or its logs. Read once and
 * taken out of the address bar, so a reload or a bookmark doesn't replay it.
 */
let pairCode: string | null = (() => {
  const m = /(?:^#|&)pair=([^&]+)/.exec(location.hash)
  if (!m) return null
  try {
    history.replaceState(null, '', location.pathname + location.search)
  } catch {
    location.hash = ''
  }
  return decodeURIComponent(m[1])
})()

/** Ask ids carry a per-page prefix: two pages (or a reload) never share one. */
const PAGE_ID = String(
  (typeof crypto !== 'undefined' && crypto.randomUUID?.()) || Math.random().toString(36).slice(2),
).slice(0, 8)

/** The permission questions open right now, by id (the `question` frames). */
export type Question = { id: string; summary: string; detail: string; hard?: boolean }
const questions = new Map<string, Question>()
export const openQuestions = () => [...questions.values()]

let onQuestionClosed: ((id: string, decision: string) => void) | null = null
export function watchQuestionClosed(fn: (id: string, decision: string) => void) {
  onQuestionClosed = fn
}

/** A question card's button: yes, no or always. */
/** The Settings panel (settings.py on the core). A secret field carries no value, only `set`. */
export type SettingField = {
  key: string
  group: string
  label: string
  secret: boolean
  help: string
  set: boolean
  environment: boolean
  value?: string
}
export type SettingsReply =
  | { type: 'settings'; fields?: SettingField[]; error?: string }
  | { type: 'settings_saved'; saved?: string[]; errors?: Record<string, string> }
let onSettings: ((reply: SettingsReply) => void) | null = null
export function watchSettings(fn: ((reply: SettingsReply) => void) | null) {
  onSettings = fn
}
const sendFrame = (frame: Record<string, unknown>): boolean => {
  if (socket?.readyState !== WebSocket.OPEN) return false
  socket.send(JSON.stringify(frame))
  return true
}
export const requestSettings = () => sendFrame({ type: 'settings_get' })
/** null or '' clears a setting. */
export const saveSettings = (values: Record<string, string | null>) => sendFrame({ type: 'settings_set', values })
export const restartCore = () => sendFrame({ type: 'settings_restart' })
/** Ask to take control of Jarvis's browser (the toggle); the server answers with browser_control. */
export const sendBrowserControl = (on: boolean) => sendFrame({ type: 'browser_control', on })
/** A pointer / key / text event for the browser (browser_input.py's shapes); ignored unless control is on. */
export const sendBrowserInput = (event: Record<string, unknown>) => sendFrame({ type: 'browser_input', event })

export function answer(q: string, decision: 'yes' | 'no' | 'always'): void {
  if (!questions.has(q) || socket?.readyState !== WebSocket.OPEN) return
  socket.send(JSON.stringify({ type: 'answer', q, decision }))
}

/**
 * "The user is here": Jarvis speaks, and draws, on the screen used most
 * recently. Throttled, since pointer and hand activity are continuous.
 */
const PRESENCE_MS = 10_000
let lastPresence = 0
export function notePresence(): void {
  const now = Date.now()
  if (now - lastPresence < PRESENCE_MS || socket?.readyState !== WebSocket.OPEN) return
  lastPresence = now
  socket.send(JSON.stringify({ type: 'presence' }))
}

if (typeof window !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') notePresence()
  })
  window.addEventListener('focus', notePresence)
  window.addEventListener('pointerdown', notePresence, { passive: true })
  window.addEventListener('pointermove', notePresence, { passive: true })
}

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

/** Resolved by the socket-level dispatcher on the first `ready` of the current
 *  connection. Re-armed per connection so a reconnect re-announces. */
let firstReady = deferred()

let everConnected = false
/** A dial has failed since the page last had a connection. */
let dialFailed = false
/** Why Jarvis turned this page away, once it has: no more dialling. */
let refusal: string | null = null

/**
 * Media tickets last 12 hours (devices.py) and a tab can stay open for days, so
 * the ticket is renewed every 6 hours with a `ticket` frame (no new hello, so
 * nothing about the session changes), and at once when a picture or /stt /tts
 * gets a 403 (config.ts). Each welcome brings a fresh one and restarts the clock.
 */
const TICKET_RENEW_MS = 6 * 60 * 60 * 1000
let renewTimer = 0
let ticketWaiters: Array<(ok: boolean) => void> = []

function scheduleRenewal() {
  clearTimeout(renewTimer)
  renewTimer = window.setTimeout(() => void renewTicket(), TICKET_RENEW_MS)
}

setTicketRenewer(
  () =>
    new Promise<boolean>((resolve) => {
      const ws = socket
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        resolve(false)
        return
      }
      const done = (ok: boolean) => {
        clearTimeout(timer)
        ticketWaiters = ticketWaiters.filter((w) => w !== done)
        resolve(ok)
      }
      const timer = setTimeout(() => done(false), 5000)
      ticketWaiters.push(done)
      ws.send(JSON.stringify({ type: 'ticket' }))
    }),
)
if (typeof document !== 'undefined') watchMediaTickets()

/** Backoff for the automatic re-dial: doubling from half a second, capped at
 *  30 s, and never giving up — Jarvis runs as a service and comes back. */
const RECONNECT_BASE_MS = 500
const RECONNECT_MAX_MS = 30_000
let attempt = 0
let reconnectTimer = 0

function scheduleReconnect() {
  if (refusal) return
  const delay = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS)
  attempt = Math.min(attempt + 1, 16)
  clearTimeout(reconnectTimer)
  reconnectTimer = window.setTimeout(() => {
    void connect().catch(() => {})
  }, delay)
}

function hello(): string {
  const token = storedToken()
  return JSON.stringify({
    type: 'hello',
    protocol: PROTOCOL_VERSION,
    client: { kind: 'face', name: 'browser' },
    caps: { display: true, gestures: true, speech: caps().stt ? 'server' : 'browser' },
    ...(token ? { token } : {}),
    ...(pairCode ? { pair: pairCode } : {}),
  })
}

/**
 * One message listener per socket, owning everything that isn't part of a
 * turn. It used to live inside warmBridge, bound to that one socket: after any
 * reconnect the SYSTEM rail froze for the life of the page, and every extra
 * warmBridge() call leaked another listener onto the same socket.
 */
function dispatch(ws: WebSocket, welcomed: () => void) {
  ws.addEventListener('message', (e: MessageEvent) => {
    let msg: Frame
    try {
      msg = JSON.parse(e.data as string)
    } catch {
      return
    }

    if (msg.type === 'welcome') {
      // Just paired: the token is what this browser shows from now on.
      if (msg.token) storeToken(msg.token)
      pairCode = null
      setMediaTicket(msg.media_ticket ?? null)
      scheduleRenewal()
      questions.clear()
      for (const q of msg.state?.questions ?? []) if (q?.id) questions.set(q.id, q)
      onIdentity?.(msg.state?.assistant)
      welcomed()
    } else if (msg.type === 'ready') {
      // Jarvis announces its servers once the hello is welcomed. Keep
      // listening so a later, more accurate list wins.
      servers = (msg.servers ?? [])
        .map((s) => (typeof s === 'string' ? s : (s.name ?? '')))
        .filter(Boolean)
      onServers?.(servers)
      onTasks?.([])   // a fresh connection: the core sends its list next, if anything is running
      firstReady.resolve()
    } else if (msg.type === 'tasks') {
      onTasks?.((Array.isArray(msg.items) ? msg.items : []).filter((t: unknown) => typeof t === 'string'))
    } else if (msg.type === 'settings' || msg.type === 'settings_saved') {
      onSettings?.(msg as SettingsReply)
    } else if (msg.type === 'ticket') {
      // The answer to a ticket frame: the new ticket (none in open mode).
      setMediaTicket(msg.media_ticket ?? null)
      scheduleRenewal()
      for (const waiter of ticketWaiters) waiter(Boolean(msg.media_ticket))
    } else if (msg.type === 'question' && msg.question?.id) {
      questions.set(msg.question.id, msg.question)
    } else if (msg.type === 'question_closed' && msg.id) {
      questions.delete(msg.id)
      onQuestionClosed?.(msg.id, msg.decision ?? '')
    } else if (msg.type === 'panel' && msg.panel) {
      onPanel?.(msg.panel)
    } else if (msg.type === 'blade' && msg.blade) {
      onBlade?.(msg.blade)
    } else if (msg.type === 'browser_control') {
      onBrowserControl?.(msg.on === true)
    } else if (msg.type === 'blade_close' && msg.id) {
      onBladeClose?.(msg.id)
    } else if (msg.type === 'capture' && msg.id) {
      const id = msg.id
      const reply = (payload: Record<string, unknown>) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'reply', id, ...payload }))
        }
      }
      if (!onCapture) {
        reply({ error: 'The interface has no camera handler.' })
      } else {
        // Always answers, even on failure: the bridge is holding a turn open
        // waiting for this, and a rejection that never arrives is a turn that
        // hangs until the idle timer notices.
        onCapture({
          mode: msg.mode === 'watch' ? 'watch' : 'look',
          reason: msg.reason ?? '',
          seconds: Math.max(2, Math.min(15, Number(msg.seconds) || 6)),
          when: msg.when === 'past' ? 'past' : 'now',
        })
          .then(reply)
          .catch((err) => reply({ error: String(err?.message ?? err) }))
      }
    } else if (msg.type === 'standby') {
      onStandby?.()
    } else if (msg.type === 'say' && msg.text) {
      onSay?.(msg.text)
    } else if (msg.type === 'ui' && msg.op) {
      // A `ui` frame with no args is normal — reset and clear take none — so an
      // absent args object is an empty one, not a reason to drop the command.
      onUi?.(msg.op, (msg.args ?? {}) as Record<string, unknown>)
    }
  })
}

function connect(): Promise<WebSocket> {
  if (refusal) return Promise.reject(new Error(refusal))
  if (socket?.readyState === WebSocket.OPEN) return Promise.resolve(socket)
  if (connecting) return connecting

  firstReady = deferred()

  connecting = new Promise<WebSocket>((resolve, reject) => {
    const ws = new WebSocket(BRIDGE_WS_URL)
    let settled = false

    /**
     * Every terminal path runs through here, and clearing `connecting` is the
     * whole point. The timeout used to reject without clearing it, which
     * bricked the client: the fast path above hands that same dead promise to
     * every later caller, so one slow start cost you a page reload.
     */
    const settle = (err: Error | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      connecting = null
      if (err) reject(err)
      else resolve(ws)
    }

    const timer = setTimeout(() => {
      ws.close()
      settle(new Error('Jarvis is not responding — is `python run_jarvis.py --core` running?'))
    }, 6000)

    // Connected once Jarvis has welcomed the hello, not when the socket opens:
    // until then it may still turn this page away.
    ws.onopen = () => {
      dispatch(ws, () => {
        socket = ws
        attempt = 0
        settle(null)
        onConnection?.(everConnected || dialFailed ? 'reconnected' : 'open')
        everConnected = true
        dialFailed = false
      })
      ws.send(hello())
    }
    ws.onerror = () => {
      /**
       * The browser will not tell us why.
       *
       * A refused handshake and a rejected Origin arrive here identically — no
       * status, no reason, just `error` — and the two have completely different
       * fixes. So say both, and put the actual port in front of them, since
       * that is the fact that distinguishes the two cases at a glance.
       */
      settle(
        new Error(
          `Cannot reach Jarvis at ${BRIDGE_WS_URL}. Either it is not ` +
            'running (start it with `python run_jarvis.py --core`), or this page is on a port it ' +
            `refuses — it accepts localhost:5173-5199 and 4173-4199, and this ` +
            `page is on ${location.port || '80'}.`,
        ),
      )
    }
    ws.onclose = (ev: CloseEvent) => {
      const refused = REFUSALS[ev.code]
      if (refused) {
        // Turned away: redialling would only be turned away again.
        refusal = refused
        if (ev.code === 4001) storeToken(null)
        clearTimeout(reconnectTimer)
        settle(new Error(refused))
        if (socket === ws) socket = null
        onConnection?.('refused', refused)
        return
      }
      // A close before the welcome is a failed dial; after it, a lost session.
      settle(new Error('Jarvis closed the connection.'))
      if (socket === ws) {
        socket = null
        onConnection?.('lost')
      } else {
        dialFailed = true
      }
      scheduleReconnect()
    }
  })

  return connecting
}

/** Open the socket early so the first "Hey Jarvis" isn't waiting on a handshake. */
export async function warmBridge(): Promise<void> {
  await connect()
  // Don't block startup if the bridge never announces — the dispatcher fills
  // the rail in whenever the list does turn up.
  await Promise.race([
    firstReady.promise,
    new Promise<void>((resolve) => setTimeout(resolve, 2500)),
  ])
}

// ---------------------------------------------------------------------------
// Turns
// ---------------------------------------------------------------------------

/**
 * No frame of any kind for two minutes means the turn is never coming back.
 * Generous on purpose: a long agent run can sit silent through a slow tool,
 * and cutting a real answer off is worse than waiting. What this catches is
 * the case that used to hang forever — the bridge alive but the turn lost.
 */
const IDLE_TIMEOUT_MS = 120_000

/** The turn in flight, so a barge-in can settle it locally. */
let pending: { finish: (fallback?: string) => void } | null = null

export async function ask(
  prompt: string,
  handlers: AskHandlers,
): Promise<{ text: string; tools: string[] }> {
  /**
   * A new question supersedes the one in flight.
   *
   * Two concurrent turns genuinely would corrupt each other — both listeners
   * see every delta, and the first 'done' resolves both with the other's text —
   * but refusing the new one was the wrong way to prevent that. It surfaced as
   * "JARVIS is already answering", which is a sentence about this module's
   * bookkeeping rather than about anything the user did, and it contradicts the
   * premise the whole app is built on: say something and it becomes the turn.
   *
   * It fired far more than it looked like it should, because the only thing
   * that cleared the slot was a barge-in — and a barge-in only fires in guard
   * mode. A transcript can arrive well after the speech that produced it: the
   * segment queue means several can be waiting, and their onsets happened while
   * the machine was still listening, when nothing interrupts. So the second
   * utterance of a normal sentence could land on a turn that was already
   * running and simply be refused.
   *
   * Cancelling settles the old promise synchronously, so by the time the code
   * below claims the slot there is nothing left to collide with. The abandoned
   * turn's caller sees its own `stale()` check and stands down quietly.
   */
  if (pending) cancel()

  // Claim the slot in this same tick. connect() below awaits, and two calls
  // made before it settles would otherwise both sail past the check above.
  let cancelledWhileDialling = false
  pending = {
    finish: () => {
      cancelledWhileDialling = true
    },
  }

  let ws: WebSocket
  try {
    ws = await connect()
  } catch (err) {
    pending = null
    throw err
  }

  // Barged in on before the socket was even up. Nothing was ever asked.
  if (cancelledWhileDialling) {
    pending = null
    return { text: '', tools: [] }
  }

  const id = `${PAGE_ID}-a${++askSeq}`
  const tools: string[] = []
  let text = ''

  return new Promise((resolve, reject) => {
    let done = false
    let timer = 0

    const cleanup = () => {
      done = true
      pending = null
      clearTimeout(timer)
      ws.removeEventListener('message', onMessage)
      ws.removeEventListener('close', onClose)
      ws.removeEventListener('error', onError)
    }

    const finish = (fallback = '') => {
      if (done) return
      cleanup()
      // Prefer the streamed text; fall back to the final result if this build
      // didn't emit deltas.
      resolve({ text: (text || fallback).trim(), tools })
    }

    const fail = (err: Error) => {
      if (done) return
      cleanup()
      reject(err)
    }

    const arm = () => {
      clearTimeout(timer)
      timer = window.setTimeout(() => {
        fail(new Error('The bridge went quiet — that turn was lost, sir.'))
      }, IDLE_TIMEOUT_MS)
    }

    const onMessage = (e: MessageEvent) => {
      // Any frame at all is proof of life, including ones this turn ignores.
      arm()

      let msg: Frame
      try {
        msg = JSON.parse(e.data as string)
      } catch {
        // A frame we can't read is not a reason to abandon the turn. It used
        // to be: the parse threw inside the listener, nothing settled the
        // promise, and App's `busy` flag stayed true for the life of the page.
        return
      }

      /**
       * Somebody else's answer.
       *
       * A superseded turn keeps streaming for a moment after it is abandoned,
       * and this listener is attached to the socket rather than to a turn — so
       * without this check the tail of the old answer is read as the beginning
       * of the new one. Measured before it existed: ask for ALPHA, barge in,
       * ask for BRAVO, and BRAVO's answer came back as "ALPHA".
       */
      if (msg.ask && msg.ask !== id) return

      try {
        switch (msg.type) {
          case 'text':
            text += msg.delta ?? ''
            handlers.onText(msg.delta ?? '')
            break

          case 'tool':
            if (!msg.name) break
            tools.push(msg.name)
            handlers.onTool(prettyToolName(msg.name))
            break

          case 'done':
            finish(msg.text ?? '')
            break

          case 'error':
            fail(new Error(msg.message ?? 'The bridge reported an error.'))
            break
        }
      } catch (err) {
        fail(err instanceof Error ? err : new Error(String(err)))
      }
    }

    const onClose = () => {
      fail(new Error('The bridge disconnected mid-answer — that session is gone.'))
    }
    const onError = () => {
      fail(new Error('The connection to the bridge failed.'))
    }

    pending = { finish }
    ws.addEventListener('message', onMessage)
    ws.addEventListener('close', onClose)
    ws.addEventListener('error', onError)
    arm()

    try {
      ws.send(JSON.stringify({ type: 'ask', text: prompt, id }))
    } catch (err) {
      // The socket can go into CLOSING between connect() resolving and here.
      fail(err instanceof Error ? err : new Error(String(err)))
    }
  })
}

/**
 * Cut JARVIS off mid-answer.
 *
 * Tells the bridge to stop, then settles the in-flight turn here rather than
 * waiting for a 'done' that a barge-in may never produce. Whatever he had
 * already said is returned, so the caller's await always comes back and the
 * transcript keeps the half-sentence the user actually heard.
 */
export function cancel(): void {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'interrupt' }))
  }
  pending?.finish()
}

/** The older name for `cancel()`. */
export function interrupt(): void {
  cancel()
}

/** `mcp__higgsfield__generate_image` -> `higgsfield · generate image` */
function prettyToolName(raw: string): string {
  if (!raw.startsWith('mcp__')) return raw
  const [, server, ...rest] = raw.split('__')
  return `${server} · ${rest.join(' ').replace(/_/g, ' ')}`
}
