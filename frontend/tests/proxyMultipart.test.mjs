import assert from 'node:assert/strict'
import test from 'node:test'
import proxy from '../api/proxy.js'

test('same-origin proxy forwards multipart bytes and boundary intact', async () => {
  const priorOrigin = process.env.BACKEND_ORIGIN
  const priorFetch = globalThis.fetch
  process.env.BACKEND_ORIGIN = 'https://backend.example'
  const body = new FormData()
  body.append('file', new Blob(['synthetic video'], {type: 'video/mp4'}), 'finished.mp4')
  body.append('revision', '11')
  let observed = false
  globalThis.fetch = async (target, options) => {
    assert.equal(target.href, 'https://backend.example/api/brands/1/content/15/youtube-assets')
    assert.equal(options.method, 'POST')
    assert.equal(options.headers.get('content-type').startsWith('multipart/form-data; boundary='), true)
    const forwarded = new Request(target, options)
    const parsed = await forwarded.formData()
    assert.equal(parsed.get('revision'), '11')
    assert.equal(await parsed.get('file').text(), 'synthetic video')
    observed = true
    return new Response(JSON.stringify({id: 1}), {status: 200, headers: {'content-type': 'application/json'}})
  }
  try {
    const request = new Request('https://dfb-social-os.vercel.app/api/proxy?backend_path=api/brands/1/content/15/youtube-assets',
      {method: 'POST', body, duplex: 'half'})
    const response = await proxy.fetch(request)
    assert.equal(response.status, 200)
    assert.equal(observed, true)
  } finally {
    globalThis.fetch = priorFetch
    if (priorOrigin === undefined) delete process.env.BACKEND_ORIGIN
    else process.env.BACKEND_ORIGIN = priorOrigin
  }
})
