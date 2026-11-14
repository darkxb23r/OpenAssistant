// How he's called: his name anywhere in what was said, and what's left once the call is taken out.
// Pure text matching (no browser APIs), so the tests in test/wake.test.ts can run it under Node.

/**
 * His name, and the only wake phrase.
 *
 * The optional prefix is genuinely optional: addressing him by name alone is
 * correct, and during an answer "Jarvis" on its own is the natural way to cut
 * in. The negative lookahead keeps possessives ("Jarvis's job") from waking him.
 *
 * The alternates are not padding. "Jarvis" is not in a general dictation
 * model's high-frequency vocabulary: Chrome routinely returns Travis, Jervis,
 * Jarvys or Java's for a perfectly clear utterance, and Whisper splits or
 * softens it (Jar vis, Charvis, Jarbis) when the room is noisy. Every one of
 * those used to be silently discarded, so the wake word "just didn't work" with
 * no indication why. Better a rare false wake than a name that does not answer.
 */
const JARVIS =
  "jarvis|jarvys|jervis|jarvis's|travis|jarviss|java's|jarv|javis|jarvus|jarvas|jarves|jarvez|jarvish|" +
  "charvis|garvis|jarbis|darvis|jar vis|j\\.a\\.r\\.v\\.i\\.s"

/**
 * How he's casually called before his name: "hey", "morning", "you there",
 * "excuse me", "quick question", "sorry", "so"... Taken off with the name, so
 * "So, Jarvis, what's on today" is just "what's on today".
 */
const GREETING =
  '(?:hey(?: there)?|hi(?:ya)?|hello|howdy|ok|okay|yo|oi|sup|what\'?s up|wassup|alright|all right|right|' +
  'so|um+|uh+|oh|ah|sorry|listen|look|say|psst|quick question|excuse me|' +
  'morning|evening|afternoon|good (?:morning|afternoon|evening|night)|(?:are )?you there)'

/** His name, and Jarvis (still answered to, out of habit), as a regex alternative. */
function names(name: string): string {
  const own = name && name.toLowerCase() !== 'jarvis' ? `${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}|` : ''
  return `(?:${own}${JARVIS})`
}

function wakeFor(name: string): RegExp {
  return new RegExp(`\\b(?:${GREETING}[\\s,.!?]*)?${names(name)}\\b(?!'s)`, 'i')
}

function overrideFor(name: string): RegExp {
  return new RegExp(`\\b(stop|wait|${names(name)}|cancel|enough|quiet|hold on|shut up|never ?mind|forget it|no)\\b`, 'i')
}

/**
 * His name, anywhere in what was said, with a greeting before it if there is
 * one. Anywhere, so he can be called the way people call each other — "Morning
 * Jarvis", "Jarvis, you there?", "check the NAS, Jarvis" — at the cost of the
 * occasional wake when someone in the room talks about him.
 */
let WAKE = wakeFor('')

/**
 * Short words that must always cut through, even when they collide with what
 * he happens to be saying. Suppressing "stop" because he just said "stop"
 * would be the single most infuriating failure this file could have.
 */
let OVERRIDE = overrideFor('')

/** What he's called (identity.py): from now on that's the wake word, and it cuts through too. */
export function setCallName(name: string): void {
  WAKE = wakeFor(name)
  OVERRIDE = overrideFor(name)
}

export const isWake = (text: string): boolean => WAKE.test(text)
export const isOverride = (text: string): boolean => OVERRIDE.test(text)

/**
 * What's left of a call that asks nothing, so he just answers "Yes, sir?" and
 * listens: "you there?", "got a minute?", "quick question", "I need you", "buddy".
 */
const CALL_ONLY = new RegExp(
  '^(?:(?:are )?you (?:there|around|awake|up|free|busy|listening|with me)|' +
    'got a (?:minute|moment|sec(?:ond)?)|(?:have you )?got a minute|quick question|' +
    "i need (?:you|something|a hand|a favou?r)|can i ask you something|wake up|" +
    "what'?s up|hello|hi|hey|yo|buddy|mate|pal|my man|my friend|old friend|sir|please)?[\\s,.!?]*$",
  'i',
)

/**
 * What was said with the call taken out: the command, wherever the name was.
 * "" when it was only a call; the text unchanged when it didn't name him.
 */
export function stripCall(text: string): string {
  const m = WAKE.exec(text)
  if (!m) return text.trim()
  const before = text.slice(0, m.index).replace(/[\s,;:-]+$/, '')
  const after = text.slice(m.index + m[0].length).replace(/^[\s,.:;!?-]+/, '')
  const rest = [before, after].filter(Boolean).join(' ').trim()
  return CALL_ONLY.test(rest) ? '' : rest
}

/** The command that came with the wake word, if any. */
export function afterWake(text: string): string {
  return WAKE.test(text) ? stripCall(text) : ''
}
