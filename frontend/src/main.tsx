import {useEffect, useState, type FormEvent} from 'react'
import {createRoot} from 'react-dom/client'
import {Activity, BarChart3, BookOpen, CalendarDays, CheckCheck, ChevronDown, ClipboardList, HardDrive, LayoutDashboard, ListTodo, LogOut, MessageCircle, Palette, Settings2, Share2, Sparkles, Tags} from 'lucide-react'
import Analytics from './pages/Analytics'
import Approvals from './pages/Approvals'
import Brands from './pages/Brands'
import Calendar from './pages/Calendar'
import Community from './pages/Community'
import Content from './pages/Content'
import Editor from './pages/Editor'
import Knowledge from './pages/Knowledge'
import Overview from './pages/Overview'
import Templates from './pages/Templates'
import Operations from './pages/Operations'
import {ToastContext} from './components/ui'
import {api, healthUrl, setCsrf} from './services/api'
import type {Brand} from './types'
import './styles.css'

const pages = [
  {name: 'Overview', icon: LayoutDashboard}, {name: 'Content', icon: Palette},
  {name: 'Calendar', icon: CalendarDays}, {name: 'Approvals', icon: CheckCheck},
  {name: 'Community', icon: MessageCircle}, {name: 'Knowledge', icon: BookOpen},
  {name: 'Analytics', icon: BarChart3}, {name: 'Templates', icon: Tags},
  {name: 'Brands', icon: Sparkles}, {name: 'Settings', icon: Settings2},
  {name: 'System', icon: Activity}, {name: 'Jobs', icon: ListTodo},
  {name: 'Publishing', icon: Share2}, {name: 'Backups', icon: HardDrive},
  {name: 'Activity', icon: ClipboardList},
]

function Login({onLogin, offline}: {onLogin: (name: string, csrf: string) => void; offline: boolean}) {
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('')
    const form = new FormData(event.currentTarget)
    try {
      const result = await api<{username: string; csrf: string}>('/auth/login', 'POST', {username: form.get('username'), password: form.get('password')})
      onLogin(result.username, result.csrf)
    } catch (reason) { setError((reason as Error).message) } finally { setBusy(false) }
  }
  return <main className="login-page"><section className="login-panel"><p className="eyebrow">DFB SOCIAL OS</p><h1>Good work,<br/><em>well managed.</em></h1><p>Sign in to your private publishing workspace.</p>{offline&&<p role="status" className="error">Local backend offline. Sign in will be available when it returns.</p>}<form onSubmit={submit}><label className="field"><span>Username</span><input name="username" autoComplete="username" required disabled={offline}/></label><label className="field"><span>Password</span><input name="password" type="password" autoComplete="current-password" required disabled={offline}/></label>{error&&<p role="alert" className="error">{error}</p>}<button className="primary full" disabled={busy||offline}>{busy?'Signing in…':'Sign in'}</button></form></section></main>
}

function App() {
  const connectedBrand = Number(new URLSearchParams(window.location.search).get('meta_connected') || 0)
  const [user, setUser] = useState(''), [brands, setBrands] = useState<Brand[]>([]), [brandId, setBrandId] = useState(connectedBrand)
  const [page, setPage] = useState(connectedBrand ? 'Settings' : 'Overview'), [contentId, setContentId] = useState<number>(), [toast, setToast] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null)
  function notify(message: string) { setToast(message); window.setTimeout(() => setToast(''), 3500) }
  function refresh() { setRefreshKey(value => value + 1) }
  function navigate(next: string, id?: number) { setContentId(id); setPage(next) }
  function authenticated(username: string, csrf: string) { setUser(username); setCsrf(csrf) }

  useEffect(() => {
    let active = true
    async function checkBackend() {
      try {
        const response = await fetch(healthUrl, {cache: 'no-store', signal: AbortSignal.timeout(5000)})
        if (active) setBackendOnline(response.ok)
      } catch {
        if (active) setBackendOnline(false)
      }
    }
    void checkBackend()
    const timer = window.setInterval(() => void checkBackend(), 15000)
    const onFocus = () => void checkBackend()
    window.addEventListener('online', onFocus)
    window.addEventListener('focus', onFocus)
    return () => {active = false; window.clearInterval(timer); window.removeEventListener('online', onFocus); window.removeEventListener('focus', onFocus)}
  }, [])

  useEffect(() => {
    const signout = () => { setUser(''); setBrands([]); setCsrf('') }
    window.addEventListener('dfb:signout', signout)
    void api<{username: string; csrf: string}>('/auth/me').then(session => authenticated(session.username, session.csrf)).catch(() => {})
    return () => window.removeEventListener('dfb:signout', signout)
  }, [])
  useEffect(() => {
    if (user) void api<Brand[]>('/brands').then(items => {setBrands(items); setBrandId(current => items.some(item => item.id === current) ? current : items[0]?.id || 0)}).catch(error => notify(error.message))
  }, [user, refreshKey])

  if (!user) return <Login onLogin={authenticated} offline={backendOnline===false}/>
  const brand = brands.find(item => item.id === brandId)
  function logout() { void api('/auth/logout', 'POST').finally(() => {setUser(''); setBrands([]); setCsrf('')}) }
  function view() {
    if (!brand) return <div className="empty"><h2>No brand configured</h2><p>Create a brand to begin.</p></div>
    switch (page) {
      case 'Overview': return <Overview brand={brand} navigate={navigate} refresh={refresh}/>
      case 'Content': return <Content brand={brand} navigate={navigate}/>
      case 'Create': return <Editor key={`${brand.id}-${contentId || 'new'}`} brand={brand} id={contentId} navigate={navigate}/>
      case 'Calendar': return <Calendar brand={brand} navigate={navigate}/>
      case 'Approvals': return <Approvals brand={brand} navigate={navigate}/>
      case 'Community': return <Community key={brand.id} brand={brand}/>
      case 'Knowledge': return <Knowledge key={brand.id} brand={brand}/>
      case 'Analytics': return <Analytics key={brand.id} brand={brand}/>
      case 'Templates': return <Templates key={brand.id} brand={brand}/>
      case 'Brands': return <Brands brand={brand} brands={brands} refresh={refresh}/>
      case 'Settings': return <Brands brand={brand} brands={brands} refresh={refresh} settings/>
      case 'System': case 'Jobs': case 'Publishing': case 'Backups': case 'Activity': return <Operations key={`${brand.id}-${page}`} brand={brand} page={page} refresh={refresh}/>
      default: return <Overview brand={brand} navigate={navigate} refresh={refresh}/>
    }
  }

  return <ToastContext.Provider value={notify}><div className="app-shell"><aside className="sidebar"><a className="wordmark" href="#" onClick={event => {event.preventDefault();navigate('Overview')}}><span>DFB</span><small>SOCIAL OS</small></a><label className="brand-picker"><span>WORKING IN</span><select aria-label="Select brand" value={brandId} onChange={event => setBrandId(Number(event.target.value))}>{brands.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select><ChevronDown size={15}/></label><nav>{pages.map(item=><button className={page===item.name?'active':''} key={item.name} onClick={()=>navigate(item.name)}><item.icon size={17}/>{item.name}</button>)}</nav><div className="sidebar-bottom"><span>{user}</span><button onClick={logout} aria-label="Sign out"><LogOut size={17}/></button></div></aside><main className="main-area"><header className="topbar"><div><small>{brand?.name || 'WORKSPACE'}</small><h1>{page==='Create'?(contentId?`Edit content #${contentId}`:'New content'):page}</h1></div><div className="topbar-status"><span className={backendOnline===false?'status-dot paused':'status-dot'}/><span>{backendOnline===false?'Local backend offline':backendOnline?'Online':'Checking backend…'}</span></div></header>{backendOnline===false?<div role="status" className="backend-offline">Local backend offline. Work requiring the backend is unavailable until the Windows service and tunnel return.</div>:<div className="page-content">{view()}</div>}</main></div>{toast&&<div role="status" className="toast">{toast}</div>}</ToastContext.Provider>
}

createRoot(document.getElementById('root')!).render(<App/>)
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => { void navigator.serviceWorker.register('/sw.js') })
}
