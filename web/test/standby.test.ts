// The reactor's standby look: dim ice-blue, and the previous settings come back on wake.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { useStore as store, STANDBY_REACTOR } from '../src/store.ts'

test('standby dims the reactor and wake restores what it was', () => {
  const s = store.getState()
  s.applyUi({ reactor: { color: '#ff0000', intensity: 2, spin: 3, style: 'wire' } })
  store.getState().enterStandbyLook()
  assert.deepEqual(
    { c: store.getState().ui.reactor.color, i: store.getState().ui.reactor.intensity },
    { c: '#4fd8ff', i: 0.3 },
  )
  assert.ok(store.getState().ui.reactor.spin < 1)
  assert.equal(store.getState().ui.reactor.style, 'wire')
  store.getState().enterStandbyLook()   // twice must not overwrite the saved look
  store.getState().leaveStandbyLook()
  const r = store.getState().ui.reactor
  assert.deepEqual([r.color, r.intensity, r.spin, r.style], ['#ff0000', 2, 3, 'wire'])
  store.getState().leaveStandbyLook()   // not on standby: no-op
  assert.equal(store.getState().ui.reactor.intensity, 2)
  assert.equal(STANDBY_REACTOR.color, '#4fd8ff')
})
