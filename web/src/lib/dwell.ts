// Pure logic for the Kinect-style dwell click: hold the pointing finger over a control and it
// clicks. Timing, tolerance, refractory and hit-testing live here with no imports, so node's test
// runner can load it. hands.ts feeds it frames and dispatches the click.

/** Total time from the finger settling on a target to the click. */
export const DWELL_DEFAULT_MS = 3000
export const DWELL_MIN_MS = 2000
export const DWELL_MAX_MS = 6000
/** The first part of the dwell: nothing is shown and nothing happens. Fixed, not a setting. */
export const DWELL_GRACE_MS = 500
/** Controls inside the permission-approval / trust UI never dwell faster than this... */
export const DWELL_GUARDED_MIN_MS = 3000
/** ...and cannot be dwell-clicked until the prompt has been up this long. */
export const DWELL_GUARDED_LOCKOUT_MS = 1000
/** The finger may wander this far (px) from where the dwell began before it restarts. */
export const DWELL_TOLERANCE_PX = 28
/** After a click, nothing dwell-clicks for this long. */
export const DWELL_REFRACTORY_MS = 1000
/** After a click, the same target is re-armed only once the finger leaves it or moves this far. */
export const DWELL_REARM_PX = 90

export type DwellSettings = { enabled: boolean; ms: number }
export const DWELL_KEY = 'jarvis.dwell'

/** A usable total dwell: anything missing or outside 2-6 s (including old, shorter stored values) becomes 3 s. */
export function clampDwellMs(ms: unknown): number {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < DWELL_MIN_MS || ms > DWELL_MAX_MS) {
    return DWELL_DEFAULT_MS
  }
  return Math.round(ms)
}

type StorageLike = { getItem(k: string): string | null; setItem(k: string, v: string): void }

export function loadDwellSettings(store: StorageLike | null | undefined): DwellSettings {
  const out: DwellSettings = { enabled: true, ms: DWELL_DEFAULT_MS }
  try {
    const raw = store?.getItem(DWELL_KEY)
    if (!raw) return out
    const j = JSON.parse(raw) as Partial<DwellSettings>
    if (typeof j.enabled === 'boolean') out.enabled = j.enabled
    out.ms = clampDwellMs(j.ms)
  } catch {
    /* corrupt or unavailable storage: defaults */
  }
  return out
}

export function saveDwellSettings(store: StorageLike | null | undefined, s: DwellSettings): void {
  try {
    store?.setItem(DWELL_KEY, JSON.stringify({ enabled: !!s.enabled, ms: clampDwellMs(s.ms) }))
  } catch {
    /* storage full or blocked: the setting just does not persist */
  }
}

/* ------------------------------------------------------------------ hit-testing */

/** The few DOM members the hit-test reads, so tests can use plain objects. */
export type ElLike = {
  tagName?: string
  parentElement?: ElLike | null
  getAttribute?(name: string): string | null
  hasAttribute?(name: string): boolean
  disabled?: boolean
  type?: string
  isContentEditable?: boolean
}

const attr = (e: ElLike, n: string) => e.getAttribute?.(n) ?? null
const tag = (e: ElLike) => (e.tagName ?? '').toLowerCase()

const CLICK_INPUT_TYPES = new Set(['button', 'submit', 'reset', 'checkbox', 'radio', 'image'])
const CLICK_ROLES = new Set(['button', 'link', 'checkbox', 'switch', 'tab', 'menuitem', 'radio'])
const TEXT_INPUT_TYPES = new Set([
  '', 'text', 'search', 'url', 'tel', 'email', 'password', 'number', 'date', 'time',
  'datetime-local', 'month', 'week',
])

/** Markers that opt a control out: destructive actions never dwell-click. */
const NO_DWELL_ATTRS = ['data-destructive', 'data-danger', 'data-no-dwell']

function isClickableSelf(e: ElLike): boolean {
  const t = tag(e)
  const role = (attr(e, 'role') ?? '').toLowerCase()
  if (t === 'button' || t === 'summary') return true
  if (t === 'a') return attr(e, 'href') !== null
  if (t === 'input') return CLICK_INPUT_TYPES.has((e.type ?? attr(e, 'type') ?? 'text').toLowerCase())
  return CLICK_ROLES.has(role)
}

function isDisabled(e: ElLike): boolean {
  return (
    e.disabled === true ||
    attr(e, 'disabled') !== null ||
    attr(e, 'aria-disabled') === 'true' ||
    attr(e, 'inert') !== null
  )
}

/**
 * The control a dwell would click for whatever is under the finger, or null. Walks up to the
 * nearest clickable ancestor, refusing if that control (or any ancestor) is disabled, inert,
 * hidden from assistive tech or marked destructive.
 */
export function dwellTarget<T extends ElLike>(el: T | null | undefined): T | null {
  let found: T | null = null
  for (let e: ElLike | null | undefined = el, n = 0; e && n < 40; e = e.parentElement, n++) {
    if (NO_DWELL_ATTRS.some((a) => attr(e as ElLike, a) !== null)) return null
    if (isDisabled(e)) {
      // A disabled control, or a disabled/inert container around the one found.
      if (found || isClickableSelf(e) || tag(e) === 'fieldset' || attr(e, 'inert') !== null) return null
    }
    if (!found && isClickableSelf(e)) found = e as T
  }
  return found
}

/** Marker on the permission-approval / trust UI; the guarded rules apply to everything inside it. */
export const GUARD_ATTR = 'data-guarded'
/** Set (performance.now() ms) on the marked container when the prompt first appeared. */
export const GUARD_SINCE_ATTR = 'data-guarded-since'

/** Is this element inside guarded UI, and since when (performance.now() ms; null if unknown)? */
export function guardOf(el: ElLike | null | undefined): { guarded: boolean; since: number | null } {
  for (let e: ElLike | null | undefined = el, n = 0; e && n < 40; e = e.parentElement, n++) {
    if (attr(e, GUARD_ATTR) !== null) {
      const v = Number(attr(e, GUARD_SINCE_ATTR))
      return { guarded: true, since: attr(e, GUARD_SINCE_ATTR) !== null && Number.isFinite(v) ? v : null }
    }
  }
  return { guarded: false, since: null }
}

/** Is focus in something the user types into? Dwell stays out of the way then. */
export function isTypingIn(active: ElLike | null | undefined): boolean {
  if (!active) return false
  const t = tag(active)
  if (t === 'textarea' || t === 'select') return true
  if (active.isContentEditable === true) return true
  const ce = attr(active, 'contenteditable')
  if (ce !== null && ce !== 'false') return true
  if (t === 'input') return TEXT_INPUT_TYPES.has((active.type ?? attr(active, 'type') ?? 'text').toLowerCase())
  return false
}

/* ------------------------------------------------------------------ the timer */

export type DwellState<T> = {
  target: T | null
  ax: number
  ay: number
  since: number
  lastClickAt: number
  /** A target just clicked, and where: re-armed once the finger leaves it or moves away. */
  blocked: { target: T; x: number; y: number } | null
}

export type DwellInput<T> = {
  enabled: boolean
  dwellMs: number
  handPresent: boolean
  gesture: string
  x: number
  y: number
  /** From dwellTarget(); null over anything not clickable. */
  target: T | null
  typing: boolean
  /** Inside the approval/trust UI: floor on the total dwell, and a lockout after the prompt appears. */
  guarded?: boolean
  guardSince?: number | null
}

export type DwellResult<T> = { state: DwellState<T>; progress: number; fire: T | null }

export const newDwell = <T>(): DwellState<T> => ({
  target: null, ax: 0, ay: 0, since: 0, lastClickAt: -Infinity, blocked: null,
})

/** One frame. Pure: returns the next state, the ring fill (0..1) and the target to click, if any. */
export function stepDwell<T>(s: DwellState<T>, i: DwellInput<T>, now: number): DwellResult<T> {
  let blocked = s.blocked
  if (blocked) {
    const away = !i.handPresent || i.target !== blocked.target ||
      Math.hypot(i.x - blocked.x, i.y - blocked.y) > DWELL_REARM_PX
    if (away) blocked = null
  }
  const idle = (): DwellResult<T> => ({
    state: { ...s, target: null, since: 0, blocked },
    progress: 0,
    fire: null,
  })
  if (!i.enabled || !i.handPresent || i.gesture !== 'point' || i.typing || !i.target) return idle()
  if (blocked || now - s.lastClickAt < DWELL_REFRACTORY_MS) return idle()
  if (i.guarded && (i.guardSince == null || now - i.guardSince < DWELL_GUARDED_LOCKOUT_MS)) return idle()

  let { target, ax, ay, since } = s
  if (target !== i.target || Math.hypot(i.x - ax, i.y - ay) > DWELL_TOLERANCE_PX) {
    target = i.target
    ax = i.x
    ay = i.y
    since = now
  }
  const total = i.guarded ? Math.max(clampDwellMs(i.dwellMs), DWELL_GUARDED_MIN_MS) : clampDwellMs(i.dwellMs)
  const elapsed = now - since
  // Nothing shows for the grace period; then the ring fills over the rest.
  const progress = elapsed <= DWELL_GRACE_MS ? 0 : Math.min(1, (elapsed - DWELL_GRACE_MS) / (total - DWELL_GRACE_MS))
  if (elapsed >= total) {
    return {
      state: { target: null, ax: 0, ay: 0, since: 0, lastClickAt: now, blocked: { target: i.target, x: i.x, y: i.y } },
      progress: 1,
      fire: i.target,
    }
  }
  return { state: { target, ax, ay, since, lastClickAt: s.lastClickAt, blocked }, progress, fire: null }
}
