import { test } from 'node:test'
import assert from 'node:assert/strict'
import { describeCameraError, detectionState, preflightFailure } from '../src/lib/gestureHealth.ts'

const ok = { secure: true, hostname: 'x', hasMediaDevices: true, wasmOk: true, modelOk: true }

test('preflight passes when everything is present', () => {
  assert.equal(preflightFailure(ok), null)
})

test('each preflight failure has its own code', () => {
  assert.equal(preflightFailure({ ...ok, secure: false, hostname: '192.168.1.5' })?.code, 'insecure')
  assert.equal(preflightFailure({ ...ok, secure: false, hostname: 'localhost' }), null)
  assert.equal(preflightFailure({ ...ok, hasMediaDevices: false })?.code, 'no-camera-api')
  assert.equal(preflightFailure({ ...ok, wasmOk: false })?.code, 'no-wasm')
  assert.equal(preflightFailure({ ...ok, modelOk: false })?.code, 'no-model')
})

test('camera errors map to guidance', () => {
  assert.equal(describeCameraError({ name: 'NotAllowedError' }).code, 'denied')
  assert.equal(describeCameraError({ name: 'NotFoundError' }).code, 'no-device')
  assert.equal(describeCameraError({ name: 'NotReadableError' }).code, 'busy')
  assert.match(describeCameraError(new Error('boom')).message, /boom/)
})

test('detection state escalates with time and reports tracking', () => {
  assert.equal(detectionState(0, '', 500).kind, 'searching')
  assert.equal(detectionState(0, '', 3500).kind, 'no-hand')
  assert.equal(detectionState(0, '', 9000).kind, 'low-light')
  assert.equal(detectionState(1, 'R:pinch', 9000).text, 'Tracking: R:pinch')
})
