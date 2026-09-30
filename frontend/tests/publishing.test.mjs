import assert from 'node:assert/strict'
import test from 'node:test'
import {normalizeDryRunResponse, normalizeDryRunResponses} from '../src/services/publishing.ts'

const planToken = 'a'.repeat(32)
const media = [{key: 'asset-1.png', preview: 'asset-1-preview.png'}, {key: 'asset-2.png'}]

function plan(platform, action, mediaCount, extra = {}) {
  return {
    platform,
    account_id: 2,
    destination_id: 'page-2',
    account_name: 'Test destination',
    action,
    caption: 'Safe preview text',
    media_count: mediaCount,
    content_id: 12,
    revision: 4,
    expires_at: '2026-09-30T08:30:00Z',
    ...extra,
  }
}

function ready(entry, extra = {}) {
  return {status: 'READY', reasons: [], plan_token: planToken, plan: [entry], ...extra}
}

test('Facebook BLOCKED dry run preserves blockers without a plan', () => {
  const result = normalizeDryRunResponse({status: 'BLOCKED', reasons: ['Page permission missing'], plan: []}, 'facebook', [])
  assert.equal(result.status, 'BLOCKED')
  assert.deepEqual(result.blockers, ['Page permission missing'])
})

test('Facebook READY text supports absent media and graph steps', () => {
  const result = normalizeDryRunResponse(ready(plan('facebook', 'Facebook Page text', 0)), 'facebook', [])
  assert.equal(result.status, 'READY')
  if (result.status !== 'READY') return
  assert.equal(result.post_type, 'TEXT')
  assert.deepEqual(result.media, [])
  assert.deepEqual(result.graph_steps, [])
})

test('Facebook READY single-image plan normalizes media', () => {
  const result = normalizeDryRunResponse(ready(plan('facebook', 'Facebook Page single image', 1)), 'facebook', media.slice(0, 1))
  assert.equal(result.status, 'READY')
  if (result.status !== 'READY') return
  assert.equal(result.post_type, 'SINGLE_IMAGE')
  assert.equal(result.media_count, 1)
  assert.equal(result.media[0].key, 'asset-1.png')
})

test('Facebook READY multi-image plan normalizes media and Graph steps', () => {
  const steps = [{method: 'POST', path: 'page/photos', purpose: 'upload_unpublished_photo'}]
  const result = normalizeDryRunResponse(ready(plan('facebook', 'Facebook Page multi image', 2, {graph_steps: steps})), 'facebook', media)
  assert.equal(result.status, 'READY')
  if (result.status !== 'READY') return
  assert.equal(result.post_type, 'MULTI_IMAGE')
  assert.equal(result.media.length, 2)
  assert.deepEqual(result.graph_steps, steps)
})

test('Instagram READY carousel remains supported', () => {
  const result = normalizeDryRunResponse(ready(plan('instagram', 'Instagram image/carousel', 2, {graph_steps: []})), 'instagram', media)
  assert.equal(result.status, 'READY')
  if (result.status !== 'READY') return
  assert.equal(result.post_type, 'CAROUSEL')
  assert.equal(result.graph_steps.length, 0)
})

test('missing optional media and blockers normalize to empty collections', () => {
  const result = normalizeDryRunResponse({status: 'READY', plan_token: planToken,
    plan: [plan('facebook', 'Facebook Page text', 0, {graph_steps: undefined})]}, 'facebook', [])
  assert.equal(result.status, 'READY')
  if (result.status !== 'READY') return
  assert.deepEqual(result.media, [])
  assert.deepEqual(result.blockers, [])
  assert.deepEqual(result.graph_steps, [])
})

test('malformed required Facebook fields become a contained platform error', () => {
  const malformed = plan('facebook', 'Facebook Page single image', 1)
  delete malformed.destination_id
  const result = normalizeDryRunResponse(ready(malformed), 'facebook', media.slice(0, 1))
  assert.equal(result.status, 'ERROR')
  if (result.status !== 'ERROR') return
  assert.equal(result.message, 'Facebook dry-run result could not be displayed.')
})

test('one malformed platform result does not blank other Editor results', () => {
  const malformedFacebook = plan('facebook', 'Facebook Page multi image', 2)
  delete malformedFacebook.media_count
  const response = ready(malformedFacebook)
  response.plan.push(plan('instagram', 'Instagram image/carousel', 2, {graph_steps: []}))
  const results = normalizeDryRunResponses(response, ['facebook', 'instagram'], media)
  assert.equal(results[0].status, 'ERROR')
  assert.equal(results[1].status, 'READY')
  if (results[0].status === 'ERROR') assert.equal(results[0].message, 'Facebook dry-run result could not be displayed.')
  if (results[1].status === 'READY') assert.equal(results[1].post_type, 'CAROUSEL')
})