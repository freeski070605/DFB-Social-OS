import assert from 'node:assert/strict'
import test from 'node:test'
import {eligibleCandidates} from '../src/services/knowledgeCandidates.ts'

const row = (id, brand_id, category, verification = 'APPROVED', enabled = true) =>
  ({id, brand_id, category, verification, enabled})

test('pillar changes never retain stale candidates, pending, disabled, or another brand', () => {
  const rows = [row(1, 1, 'grocery & food savings'), row(2, 1, 'routines & weekly resets'),
    row(3, 1, 'routines & weekly resets', 'PENDING'), row(4, 1, 'routines & weekly resets', 'APPROVED', false),
    row(5, 2, 'routines & weekly resets')]
  assert.deepEqual(eligibleCandidates(rows, 1, 'grocery & food savings').map(item => item.id), [1])
  assert.deepEqual(eligibleCandidates(rows, 1, ' routines  & weekly resets ').map(item => item.id), [2])
  assert.deepEqual(eligibleCandidates([rows[0]], 1, 'routines & weekly resets'), [])
})
