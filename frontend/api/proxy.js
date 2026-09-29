const excludedRequestHeaders = new Set([
  'host', 'connection', 'content-length', 'transfer-encoding',
  'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-port', 'x-forwarded-proto',
])
const excludedResponseHeaders = new Set([
  'connection', 'content-encoding', 'content-length', 'transfer-encoding',
])

export default {
  async fetch(request) {
    const originValue = process.env.BACKEND_ORIGIN
    if (!originValue) return new Response('Backend origin is not configured', {status: 503})

    let origin
    try {
      origin = new URL(originValue)
      if (origin.protocol !== 'https:' || origin.username || origin.password ||
          origin.pathname !== '/' || origin.search || origin.hash) throw new Error('Invalid origin')
    } catch {
      return new Response('Backend origin is invalid', {status: 503})
    }

    const incoming = new URL(request.url)
    const path = incoming.searchParams.get('backend_path')
    if (!path || (path !== 'health' && !path.startsWith('api/')) ||
        path.split('/').some(segment => segment === '.' || segment === '..' || segment.includes('\\'))) {
      return new Response('Invalid backend path', {status: 400})
    }

    incoming.searchParams.delete('backend_path')
    const target = new URL(`/${path}${incoming.search}`, origin)
    if (target.origin !== origin.origin ||
        (target.pathname !== '/health' && !target.pathname.startsWith('/api/'))) {
      return new Response('Invalid backend path', {status: 400})
    }

    const headers = new Headers(request.headers)
    for (const name of excludedRequestHeaders) headers.delete(name)
    const hasBody = request.method !== 'GET' && request.method !== 'HEAD'

    try {
      const upstream = await fetch(target, {
        method: request.method,
        headers,
        body: hasBody ? request.body : undefined,
        duplex: hasBody ? 'half' : undefined,
        redirect: 'manual',
      })
      const responseHeaders = new Headers(upstream.headers)
      for (const name of excludedResponseHeaders) responseHeaders.delete(name)
      const location = responseHeaders.get('location')
      if (location) {
        const redirect = new URL(location, origin)
        if (redirect.origin === origin.origin) {
          responseHeaders.set('location', `${incoming.origin}${redirect.pathname}${redirect.search}${redirect.hash}`)
        }
      }
      responseHeaders.set('cache-control', 'no-store')
      return new Response(upstream.body, {status: upstream.status, headers: responseHeaders})
    } catch {
      return new Response('Backend unavailable', {status: 502})
    }
  },
}
