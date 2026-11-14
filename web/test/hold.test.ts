// Run with `npm test` (Node's own test runner; Node 22.18+ strips the types).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { asAnswer, drainReady, isAudible, voiceMode } from '../src/lib/hold.ts'

test('nothing is heard while he speaks aloud, in any phase', () => {
  for (const phase of ['dormant', 'waking', 'listening', 'thinking', 'tooling', 'speaking'] as const) {
    assert.equal(voiceMode(phase, true, false), 'hold')
    assert.equal(voiceMode(phase, true, true), 'hold')
  }
  assert.equal(voiceMode('offline', true, false), 'deaf')
})

test('by voice, quiet phases map as before', () => {
  assert.equal(voiceMode('dormant', false, false), 'wake')
  assert.equal(voiceMode('listening', false, false), 'command')
  assert.equal(voiceMode('waking', false, false), 'command')
  assert.equal(voiceMode('thinking', false, false), 'guard')
  assert.equal(voiceMode('tooling', false, false), 'guard')
})

test('in text mode a busy, quiet page only listens for his name', () => {
  assert.equal(voiceMode('thinking', false, true), 'wake')
  assert.equal(voiceMode('tooling', false, true), 'wake')
  assert.equal(voiceMode('dormant', false, true), 'wake')
})

test('the speaking phase counts as audible, unless a question waits on the user', () => {
  assert.equal(isAudible(false, 'speaking', false), true)
  assert.equal(isAudible(false, 'speaking', true), false)
  assert.equal(isAudible(true, 'speaking', true), true)
  assert.equal(isAudible(false, 'thinking', false), false)
})

test('never held while he waits for the user: the greeting after his name, the follow-up window', () => {
  assert.equal(isAudible(true, 'waking', false), false)
  assert.equal(isAudible(true, 'listening', false), false)
  assert.equal(voiceMode('listening', isAudible(true, 'listening', false), false), 'command')
})

test('held lines go once he is quiet and the turn is over, or a question waits', () => {
  assert.equal(drainReady(true, false, false), false)
  assert.equal(drainReady(true, true, true), false)
  assert.equal(drainReady(false, true, false), false)
  assert.equal(drainReady(false, false, false), true)
  assert.equal(drainReady(false, true, true), true)
})

test('typed answers parse like the core parses them', () => {
  assert.equal(asAnswer('yes'), 'yes')
  assert.equal(asAnswer('Yes please'), 'yes')
  assert.equal(asAnswer('go ahead'), 'yes')
  assert.equal(asAnswer('yes, always'), 'always')
  assert.equal(asAnswer('always'), 'always')
  assert.equal(asAnswer("don't"), 'no')
  assert.equal(asAnswer('hold off'), 'no')
  assert.equal(asAnswer('what is the weather'), null)
  assert.equal(asAnswer(''), null)
})
