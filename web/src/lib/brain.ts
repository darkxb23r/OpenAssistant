import * as bridge from './bridge'
import type { AskHandlers } from './bridge'
import type { Blade, Panel } from '../store'

export type { AskHandlers }
export type { ConnectionState } from './bridge'

/**
 * The brain is Jarvis (the Python core), over its socket (bridge.ts). The
 * conversation lives there, so the page sends one line at a time and streams
 * the answer back; every tool, server and permission check is Jarvis's.
 */

export async function ask(
  prompt: string,
  handlers: AskHandlers,
): Promise<{ text: string; tools: string[] }> {
  return bridge.ask(prompt, handlers)
}

export async function warm(): Promise<void> {
  await bridge.warmBridge()
}

/** Jarvis reports its server list on connect, so the HUD subscribes. */
export function watchServers(fn: (servers: string[]) => void): void {
  bridge.watchServers(fn)
}

/** What he's working on (tasks and research, by name), whenever it changes. */
export function watchTasks(fn: (tasks: string[]) => void): void {
  bridge.watchTasks(fn)
}

/** HUD panels are pushed mid-turn by the `display` tool, not returned by ask(). */
export function watchPanels(fn: (panel: Panel) => void): void {
  bridge.watchPanels(fn)
}

/** Blades — the big surface — arrive the same way, from the `blade` tool. */
export function watchBlades(fn: (blade: Blade) => void): void {
  bridge.watchBlades(fn)
}

/** What he's called, each time the core welcomes this page. */
export function watchIdentity(fn: (raw: unknown) => void): void {
  bridge.watchIdentity(fn)
}

/** A blade Jarvis takes down himself (the browser's live view). */
export function watchBladeClose(fn: (id: string) => void): void {
  bridge.watchBladeClose(fn)
}

/**
 * Redressing the interface — theme, reactor, orbiting objects, effects. The
 * `ui_*` tools (ui_tools.py) push these straight down the open socket, mid-turn.
 */
export function watchUi(fn: (op: string, args: any) => void): void {
  bridge.watchUi(fn)
}

/**
 * The one thing Jarvis asks US for: a camera frame. The hardware is in the
 * browser and the model is in Jarvis, so this handler answers a request rather
 * than receiving a push.
 */
export function watchCapture(
  fn: (req: bridge.CaptureRequest) => Promise<bridge.CaptureResult>,
): void {
  bridge.watchCapture(fn)
}

/**
 * Barge-in. Interrupts Jarvis and settles whatever `ask()` call is
 * outstanding with the text so far, so the caller's await always returns.
 */
export function cancel(): void {
  bridge.cancel()
}

/** The older name for `cancel()`. */
export function interrupt(): void {
  cancel()
}

/** Whether Jarvis is reachable right now: a live socket is the session. */
export function isConnected(): boolean {
  return bridge.isConnected()
}

/**
 * Connection state, for the UI. Worth surfacing because anything Jarvis says
 * while the socket is down is not heard here, and a refused page needs pairing.
 */
export function watchConnection(
  fn: (state: bridge.ConnectionState, message?: string) => void,
): void {
  bridge.watchConnection(fn)
}

/** Speech that arrives between turns (see bridge.watchSay). */
export function watchSay(fn: (text: string) => void): void {
  bridge.watchSay(fn)
}

/** Jarvis went on standby (see bridge.watchStandby). */
export function watchStandby(fn: () => void): void {
  bridge.watchStandby(fn)
}

/** A permission question was answered (anywhere): its card comes down. */
export function watchQuestionClosed(fn: (id: string, decision: string) => void): void {
  bridge.watchQuestionClosed(fn)
}

/** The permission questions open right now (oldest first). */
export function openQuestions(): bridge.Question[] {
  return bridge.openQuestions()
}

/** A question card's button. */
export function answer(q: string, decision: 'yes' | 'no' | 'always'): void {
  bridge.answer(q, decision)
}

/** The user is here (hand tracking sees them, say): throttled in bridge.ts. */
export function notePresence(): void {
  bridge.notePresence()
}

/** Labels for the HUD's SYSTEMS rail. */
export function connectedLabels(): string[] {
  return bridge.bridgeServers()
}
