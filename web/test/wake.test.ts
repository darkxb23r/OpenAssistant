// How he's called: his name anywhere, the greeting and the call taken off, the rest is the command.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { afterWake, isWake, setCallName, stripCall } from '../src/lib/wake.ts'

test('his name alone, or with a call that asks nothing, wakes him with nothing to do yet', () => {
  for (const said of ['Jarvis.', 'Jarvis?', 'Hey Jarvis', 'Morning, Jarvis.', 'Jarvis, you there?',
    'Jarvis, got a minute?', 'Jarvis, quick question.', 'Yo Jarvis', "What's up, Jarvis?",
    'Jarvis, I need you.', 'Sorry, Jarvis, you busy?', 'Jarvis, buddy']) {
    assert.ok(isWake(said), said)
    assert.equal(stripCall(said), '', said)
  }
})

test('his name with a request: the request is what is left, wherever the name was', () => {
  assert.equal(stripCall("Jarvis, what's on today?"), "what's on today?")
  assert.equal(stripCall("So, Jarvis, what's on today?"), "what's on today?")
  assert.equal(stripCall('Um Jarvis can you check the NAS'), 'can you check the NAS')
  assert.equal(stripCall('Check the NAS, Jarvis.'), 'Check the NAS')
  assert.equal(stripCall('Good evening Jarvis, play some music'), 'play some music')
  assert.equal(afterWake('Jarvis, turn the lights off'), 'turn the lights off')
})

test('the ways speech-to-text mishears the name still wake him', () => {
  for (const said of ['Jar vis, are you there?', 'Charvis, what time is it?', 'Jarbis.', 'Travis, you up?']) {
    assert.ok(isWake(said), said)
  }
})

test('not naming him does not', () => {
  // ("Jarvis's" does wake him: Chrome hears it for a plain "Jarvis", so it's on the list.)
  assert.ok(!isWake('what time is it') && !isWake('the javascript file'))
  assert.equal(afterWake('what time is it'), '')
})

test('a new name becomes the wake word, and Jarvis still works', () => {
  setCallName('Friday')
  assert.ok(isWake('Friday, you there?') && isWake('Jarvis?'))
  assert.equal(stripCall('Hey Friday, dim the lights'), 'dim the lights')
  setCallName('')
})
