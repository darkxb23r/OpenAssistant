import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DWELL_DEFAULT_MS, DWELL_GRACE_MS, DWELL_GUARDED_LOCKOUT_MS, DWELL_REFRACTORY_MS, guardOf, DWELL_TOLERANCE_PX, clampDwellMs, dwellTarget, isTypingIn,
  loadDwellSettings, newDwell, saveDwellSettings, stepDwell, type DwellInput, type ElLike,
} from '../src/lib/dwell.ts'

const el = (tagName: string, attrs: Record<string, string> = {}, parent: ElLike | null = null, extra: Partial<ElLike> = {}): ElLike => ({
  tagName, parentElement: parent, getAttribute: (n) => (n in attrs ? attrs[n] : null), ...extra,
})
const btn = el('BUTTON')
const base: DwellInput<ElLike> = { enabled: true, dwellMs: 3000, handPresent: true, gesture: 'point', x: 100, y: 100, target: btn, typing: false }
const run = (frames: [number, Partial<DwellInput<ElLike>>][]) => {
  let s = newDwell<ElLike>()
  const out = []
  for (const [t, o] of frames) { const r = stepDwell(s, { ...base, ...o }, t); s = r.state; out.push(r) }
  return out
}

test('settings: defaults, clamp, persistence', () => {
  assert.deepEqual(loadDwellSettings(null), { enabled: true, ms: DWELL_DEFAULT_MS })
  assert.equal(DWELL_DEFAULT_MS, 3000)
  assert.equal(clampDwellMs(1200), 3000) // old stored value migrates to 3 s
  assert.equal(clampDwellMs(99999), 3000)
  assert.equal(clampDwellMs('x'), 3000)
  assert.equal(clampDwellMs(2000), 2000)
  assert.equal(clampDwellMs(6000), 6000)
  const m = new Map<string, string>()
  const st = { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) }
  saveDwellSettings(st, { enabled: false, ms: 4000 })
  assert.deepEqual(loadDwellSettings(st), { enabled: false, ms: 4000 })
  m.set('jarvis.dwell', '{"enabled":true,"ms":1200}')
  assert.equal(loadDwellSettings(st).ms, 3000)
  m.set('jarvis.dwell', '{bad')
  assert.equal(loadDwellSettings(st).enabled, true)
})

test('hit-test: clickable things', () => {
  assert.ok(dwellTarget(btn))
  assert.ok(dwellTarget(el('A', { href: '/x' })))
  assert.equal(dwellTarget(el('A')), null)
  assert.ok(dwellTarget(el('INPUT', { type: 'checkbox' })))
  assert.equal(dwellTarget(el('INPUT', { type: 'text' })), null)
  assert.ok(dwellTarget(el('DIV', { role: 'button' })))
  assert.equal(dwellTarget(el('DIV')), null)
  const b = el('BUTTON'); assert.equal(dwellTarget(el('SPAN', {}, b)), b)
})

test('hit-test: disabled, destructive, inert refused', () => {
  assert.equal(dwellTarget(el('BUTTON', {}, null, { disabled: true })), null)
  assert.equal(dwellTarget(el('BUTTON', { 'aria-disabled': 'true' })), null)
  assert.equal(dwellTarget(el('BUTTON', { 'data-destructive': '' })), null)
  assert.equal(dwellTarget(el('BUTTON', {}, el('DIV', { 'data-danger': '1' }))), null)
  assert.equal(dwellTarget(el('BUTTON', {}, el('FIELDSET', { disabled: '' }))), null)
})

test('typing detection', () => {
  assert.ok(isTypingIn(el('INPUT', { type: 'text' })))
  assert.ok(isTypingIn(el('TEXTAREA')))
  assert.ok(isTypingIn(el('DIV', {}, null, { isContentEditable: true })))
  assert.ok(!isTypingIn(el('BUTTON')))
  assert.ok(!isTypingIn(el('INPUT', { type: 'checkbox' })))
  assert.ok(!isTypingIn(null))
})

test('grace: nothing for 0.5 s, ring fills over the remaining 2.5 s, clicks at 3 s', () => {
  const r = run([[0, {}], [DWELL_GRACE_MS, {}], [1750, {}], [2999, {}], [3000, {}]])
  assert.equal(r[0].progress, 0)
  assert.equal(r[1].progress, 0)
  assert.ok(Math.abs(r[2].progress - 0.5) < 1e-9)
  assert.equal(r[3].fire, null)
  assert.ok(r[3].progress < 1)
  assert.equal(r[4].fire, btn)
  assert.equal(r[4].progress, 1)
})

test('resets on move beyond tolerance, jitter within is fine', () => {
  const r = run([[0, {}], [1500, { x: 100 + DWELL_TOLERANCE_PX - 1 }], [3000, { x: 100 + DWELL_TOLERANCE_PX - 1 }]])
  assert.equal(r[2].fire, btn)
  const r2 = run([[0, {}], [1500, { x: 100 + DWELL_TOLERANCE_PX + 5 }], [3000, { x: 100 + DWELL_TOLERANCE_PX + 5 }]])
  assert.equal(r2[2].fire, null)
  assert.equal(r2[2].progress, 0.0 + r2[2].progress); assert.ok(r2[2].progress < 1)
})

test('resets on leaving element, gesture change, hand loss, typing, disabled toggle', () => {
  for (const o of [{ target: null }, { target: el('BUTTON') }, { gesture: 'open' }, { handPresent: false }, { typing: true }, { enabled: false }]) {
    const r = run([[0, {}], [2000, o], [2300, {}]])
    assert.equal(r[1].progress, 0)
    assert.equal(r[2].fire, null, JSON.stringify(Object.keys(o)))
  }
})

test('refractory and re-arm: no machine-gun on same target', () => {
  let s = newDwell<ElLike>()
  let r = stepDwell(s, base, 0); s = r.state
  r = stepDwell(s, base, 3000); s = r.state
  assert.equal(r.fire, btn)
  for (const t of [3100, 4000, 5000, 9000]) { r = stepDwell(s, base, t); s = r.state; assert.equal(r.fire, null); assert.equal(r.progress, 0) }
  // leave and come back after refractory
  r = stepDwell(s, { ...base, target: null }, 9100); s = r.state
  r = stepDwell(s, base, 9200); s = r.state
  r = stepDwell(s, base, 9200 + 3000); assert.equal(r.fire, btn)
})

test('moving far away re-arms the same target; refractory still holds', () => {
  let s = newDwell<ElLike>()
  s = stepDwell(s, base, 0).state
  s = stepDwell(s, base, 3000).state
  const far = { ...base, x: 400 }
  let r = stepDwell(s, far, 3000 + DWELL_REFRACTORY_MS - 10); s = r.state
  assert.equal(r.progress, 0)
  const t0 = 3000 + DWELL_REFRACTORY_MS
  s = stepDwell(s, far, t0).state
  r = stepDwell(s, far, t0 + 3000)
  assert.equal(r.fire, btn)
})

test('dwell length follows setting', () => {
  const r = run([[0, { dwellMs: 2000 }], [1999, { dwellMs: 2000 }], [2000, { dwellMs: 2000 }]])
  assert.equal(r[1].fire, null)
  assert.equal(r[2].fire, btn)
  const r6 = run([[0, { dwellMs: 6000 }], [5999, { dwellMs: 6000 }], [6000, { dwellMs: 6000 }]])
  assert.equal(r6[1].fire, null)
  assert.equal(r6[2].fire, btn)
})

test('guarded (approval/trust) controls: 3 s floor and 1 s lockout after the prompt appears', () => {
  const g = (since: number | null) => ({ guarded: true, guardSince: since, dwellMs: 2000 })
  // slider at 2 s, but guarded needs 3 s
  let r = run([[0, g(-5000)], [2000, g(-5000)], [2999, g(-5000)], [3000, g(-5000)]])
  assert.equal(r[1].fire, null)
  assert.equal(r[2].fire, null)
  assert.equal(r[3].fire, btn)
  // prompt appeared at t=0: within 1 s nothing counts; timer only starts after lockout
  r = run([[0, g(0)], [500, g(0)], [DWELL_GUARDED_LOCKOUT_MS, g(0)], [DWELL_GUARDED_LOCKOUT_MS + 2999, g(0)], [DWELL_GUARDED_LOCKOUT_MS + 3000, g(0)]])
  assert.equal(r[1].progress, 0)
  assert.equal(r[3].fire, null)
  assert.equal(r[4].fire, btn)
  // unknown prompt age: refuse
  r = run([[0, g(null)], [10000, g(null)]])
  assert.equal(r[1].fire, null)
  // marker lookup walks ancestors
  const box = el('DIV', { 'data-guarded': '', 'data-guarded-since': '42' })
  assert.deepEqual(guardOf(el('BUTTON', {}, box)), { guarded: true, since: 42 })
  assert.equal(guardOf(btn).guarded, false)
  // a longer slider still wins
  r = run([[0, { guarded: true, guardSince: -5000, dwellMs: 5000 }], [3000, { guarded: true, guardSince: -5000, dwellMs: 5000 }]])
  assert.equal(r[1].fire, null)
})
