import assert from 'node:assert/strict'
import test from 'node:test'
import {readableError} from '../src/services/validation.ts'

test('FastAPI slide validation becomes an actionable draft message', () => {
  const detail = [{loc: ['body', 'slides', 0, 'title'], type: 'string_too_short',
                   msg: 'String should have at least 1 character'}]
  assert.equal(readableError(new Error(JSON.stringify(detail))), 'Slide 1 title is required')
})

test('enum validation and ordinary errors remain readable', () => {
  assert.equal(readableError(new Error(JSON.stringify([{loc: ['body', 'format'], type: 'literal_error'}]))),
               'format has an unsupported value')
  assert.equal(readableError(new Error(JSON.stringify([{loc: ['body', 'body'], type: 'string_too_long'}]))),
               'body is too long')
  assert.equal(readableError(new Error(JSON.stringify([{loc: ['body', 'topic'], type: 'string_too_short'}]))),
               'topic is too short')
  assert.equal(readableError(new Error('Pillar must match a configured pillar')),
               'Pillar must match a configured pillar')
})
