import assert from 'node:assert/strict'
import test from 'node:test'
import {visibleYoutubeState} from '../src/services/youtubeStatus.ts'

const upload = {state: 'PUBLISHED', provider_video_id: 'saved-video'}

test('completed bytes with stale PROCESSING receipt remain PROCESSING in UI', () => {
  assert.equal(visibleYoutubeState(upload, {state: 'PROCESSING', external_id: 'saved-video'}), 'PROCESSING')
  assert.equal(visibleYoutubeState(upload, undefined), 'PROCESSING')
})

test('UI shows PUBLISHED only when matching receipt is PUBLISHED', () => {
  assert.equal(visibleYoutubeState(upload, {state: 'PUBLISHED', external_id: 'saved-video'}), 'PUBLISHED')
  assert.equal(visibleYoutubeState(upload, {state: 'PUBLISHED', external_id: 'different-video'}), 'PROCESSING')
})

test('reconciliation and failure states stay visible', () => {
  assert.equal(visibleYoutubeState({...upload, state: 'RECONCILIATION_REQUIRED'},
    {state: 'RECONCILIATION_REQUIRED', external_id: 'saved-video'}), 'RECONCILIATION_REQUIRED')
  assert.equal(visibleYoutubeState({...upload, state: 'FAILED'},
    {state: 'FAILED', external_id: 'saved-video'}), 'FAILED')
})
