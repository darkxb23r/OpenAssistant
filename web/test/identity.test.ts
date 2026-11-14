// The name the core sends (identity.py), and the wake word that follows it.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { brandMark, readIdentity, DEFAULT_IDENTITY } from '../src/lib/identity.ts'

test('unset or unusable: Jarvis, with its mark and meaning', () => {
  assert.deepEqual(readIdentity(undefined), DEFAULT_IDENTITY)
  assert.deepEqual(readIdentity({ name: '  ' }), DEFAULT_IDENTITY)
  assert.equal(brandMark(DEFAULT_IDENTITY), 'J.A.R.V.I.S.')
})

test('a new name is shown as it is, its meaning under it', () => {
  const who = readIdentity({ name: 'Friday', meaning: 'Female Replacement Intelligent Digital Assistant Youth' })
  assert.equal(brandMark(who), 'FRIDAY')
  assert.equal(who.meaning, 'Female Replacement Intelligent Digital Assistant Youth')
  assert.equal(readIdentity({ name: 'Edith' }).meaning, '')
})
