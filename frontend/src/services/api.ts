let csrf = ''
export function setCsrf(value: string) { csrf = value }
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {method, credentials: 'include', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: body === undefined ? undefined : JSON.stringify(body)})
  const data = await response.json()
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('dfb:signout'))
    throw new Error(detail || 'Request failed')
  }
  return data as T
}
export async function apiUpload<T>(path: string, body: FormData): Promise<T> {
  const response = await fetch('/api' + path, {method: 'POST', credentials: 'include', headers: {'X-CSRF-Token': csrf}, body})
  const data = await response.json()
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    if (response.status === 401) window.dispatchEvent(new Event('dfb:signout'))
    throw new Error(detail || 'Request failed')
  }
  return data as T
}
export const brandPath = (id: number, path: string) => `/brands/${id}${path}`
export function date(value?: string | null) { return value ? new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z').toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : 'Unscheduled' }
export const formats = ['carousel','single_graphic','checklist','steps','do_dont','comparison','tip','story','reel_script','short_video_script','text_post']
export const kinds = ['cover','checklist','steps','two_column','do_dont','statement','tip','end']
export const label = (s: string) => s.replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase())
