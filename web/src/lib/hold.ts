import type { Phase } from '../store'
import type { VoiceMode } from './voice'

/**
 * When the user's words wait for Jarvis, and when the mic opens.
 *
 * Pure rules, no React, so they can be tested on their own (web/test/hold.test.ts).
 *
 * Holding: while he is speaking aloud nothing the user says or types reaches him. Talking over him
 * used to stop the answer and start a new turn on top of the words still in the air, which is how he
 * ended up speaking twice. Speech heard while he talks is dropped; a typed line waits and goes as
 * soon as he has finished. Escape and the stop button still cut him off on purpose.
 *
 * Text mode: after the user types, the mic doesn't open for a follow-up. The page waits for his name
 * (a wake word has to listen for it) and stays text-based until he's called by voice.
 */

const BUSY: readonly Phase[] = ['thinking', 'tooling', 'speaking']

export const isBusy = (phase: Phase) => BUSY.includes(phase)

/**
 * Whether he is speaking aloud, for holding: while he answers or tells the user something. The
 * speakers' own signal (`tts.audible()`) covers the tool filler; the 'speaking' phase covers the
 * moments between two streamed sentences.
 *
 * Never while he's waiting for the user (waking, listening): his "Yes, sir?" after his name plays
 * while the mic is open on purpose, and people start the command straight over it. Holding then
 * dropped the command and the page timed out to standby with the words still on screen. The echo
 * filter keeps his own voice out there, as it always has.
 *
 * Not while a permission question is open either: the turn then waits on the answer with the
 * phase still 'speaking', and once the question has been said the user must be heard.
 */
export function isAudible(ttsAudible: boolean, phase: Phase, questionOpen: boolean): boolean {
  if (phase === 'waking' || phase === 'listening') return false
  return ttsAudible || (phase === 'speaking' && !questionOpen)
}

/** What the voice loop should do with what it hears. */
export function voiceMode(phase: Phase, audible: boolean, textMode: boolean): VoiceMode {
  if (phase === 'offline' || phase === 'boot') return 'deaf'
  if (audible) return 'hold'
  if (phase === 'dormant') return 'wake'
  if (phase === 'waking' || phase === 'listening') return 'command'
  // Busy (thinking, tooling) and quiet: in text mode only his name counts; by voice, talking over
  // him still stops the turn, as before.
  return textMode ? 'wake' : 'guard'
}

/**
 * Whether the lines typed while he spoke can go now: he's quiet, and either the turn is over or it
 * is waiting on a permission question (which those lines may well answer).
 */
export function drainReady(audible: boolean, turnBusy: boolean, questionOpen: boolean): boolean {
  return !audible && (!turnBusy || questionOpen)
}

const YES = new Set(['y', 'yes', 'yeah', 'yep', 'sure', 'ok', 'okay', 'affirmative', 'allow', 'approve', 'approved'])
const NO = new Set(['n', 'no', 'nope', 'nah', 'not', "don't", 'dont', 'deny', 'denied', 'decline', 'declined',
  'stop', 'cancel', 'never', 'negative', 'skip', 'refuse'])

/**
 * A typed answer to a permission question: 'yes', 'no', 'always', or null when the line is something
 * else. The same words as the core's guardrails.parse_answer (and satellite.py's copy of it).
 */
export function asAnswer(text: string): 'yes' | 'no' | 'always' | null {
  const words = text.toLowerCase().match(/[a-z']+/g) ?? []
  const first = words[0]
  if (first === undefined) return null
  const two = words.slice(0, 2).join(' ')
  if (first === 'always') return 'always'
  if (YES.has(first) || two === 'go ahead' || two === 'do it') {
    return words.slice(1, 3).includes('always') ? 'always' : 'yes'
  }
  if (NO.has(first) || two === 'do not' || two === 'hold off') return 'no'
  return null
}
