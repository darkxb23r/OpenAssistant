/**
 * What the assistant is called and what the name means, from the core's welcome frame
 * (identity.py: JARVIS_NAME and JARVIS_NAME_MEANING). Jarvis until the core says otherwise.
 */
export type Identity = { name: string; meaning: string }

/** The longest meaning the core keeps (identity.py MEANING_MAX). */
export const MEANING_MAX = 750

export const DEFAULT_IDENTITY: Identity = { name: 'Jarvis', meaning: 'Just A Rather Very Intelligent System' }

/** The HUD's brand: Jarvis keeps its dotted mark; another name is shown as it is, in capitals. */
export function brandMark({ name }: Identity): string {
  return name.toLowerCase() === 'jarvis' ? 'J.A.R.V.I.S.' : name.toUpperCase()
}

/** A name the core sent, or the default for anything unusable. */
export function readIdentity(raw: unknown): Identity {
  const a = (raw ?? {}) as Partial<Identity>
  const name = typeof a.name === 'string' ? a.name.trim().slice(0, 32) : ''
  if (!name) return DEFAULT_IDENTITY
  return { name, meaning: typeof a.meaning === 'string' ? a.meaning.trim().slice(0, MEANING_MAX) : '' }
}
