import {useState} from 'react'
import {api,brandPath,label} from '../services/api'
import {Panel,useAction} from '../components/ui'
import {useData} from '../hooks/useData'
import type {Account} from '../types'

type PlatformState={platform:string;support:string;connection:string;capabilities:string[];available_capabilities:string[];account_count:number}
type Identity={platform:string;display_name:string;username:string}

function IdentityEditor({brandId,platform,initial,reload}:{brandId:number;platform:string;initial?:Identity;reload:()=>Promise<unknown>}){
 const [displayName,setDisplayName]=useState(initial?.display_name||'')
 const [username,setUsername]=useState(initial?.username||'')
 const {busy,run}=useAction()
 return <form onSubmit={e=>{e.preventDefault();void run(async()=>{await api(brandPath(brandId,`/social-identities/${platform}`),'PUT',{platform,display_name:displayName,username});await reload()},'Public identity saved')}}><label>Display name <input value={displayName} onChange={e=>setDisplayName(e.target.value)}/></label><label>Username or handle <input value={username} onChange={e=>setUsername(e.target.value)} placeholder="Enter only if known"/></label><button disabled={busy}>Save identity</button></form>
}

export default function MetaAccounts({brandId}:{brandId:number}){
 const {busy,run}=useAction()
 const {data:accounts,reload}=useData<Account[]>(brandPath(brandId,'/accounts'))
 const {data:platforms,reload:reloadPlatforms}=useData<PlatformState[]>(brandPath(brandId,'/platforms'))
 const {data:identities,reload:reloadIdentities}=useData<Identity[]>(brandPath(brandId,'/social-identities'))
 async function connect(platform:'meta'|'youtube'){
  const result=await api<{authorization_url:string}>(brandPath(brandId,`/${platform}/connect`),'POST')
  window.location.assign(result.authorization_url)
 }
 async function refresh(){await reload();await reloadPlatforms()}
 return <div className="columns"><Panel title="Platform connections"><div className="button-row"><button className="primary" disabled={busy} onClick={()=>run(()=>connect('meta'),'Opening Meta authorization')}>Connect Meta</button><button disabled={busy} onClick={()=>run(()=>connect('youtube'),'Opening YouTube authorization')}>Connect YouTube</button></div><p className="muted">OAuth discovers accounts. Choose a verified destination explicitly. YouTube currently requests read-only channel identity; uploads are unavailable.</p>{platforms?.map(p=><div className="account-row" key={p.platform}><h3>{label(p.platform)}</h3><p>{p.support==='PLANNED'?'Planned · not yet implemented':label(p.connection)}</p><p>Available actions: {p.available_capabilities.length?p.available_capabilities.map(label).join(', '):'None yet'}</p><IdentityEditor brandId={brandId} platform={p.platform} initial={identities?.find(i=>i.platform===p.platform)} reload={reloadIdentities}/><small>Manually entered identity; does not verify or connect this account.</small></div>)}</Panel><Panel title="Discovered accounts">{accounts?.map(a=><div className="account-row" key={a.id}><h3>{a.name||label(a.platform)} · {label(a.platform)}</h3><p>ID: {a.account_id} · {a.source==='oauth'?'OAuth':'Legacy credential'} · {a.enabled?'Selected':'Not selected'}</p><p>Connection: {label(a.token_status)} · Last checked: {a.last_checked?new Date(a.last_checked).toLocaleString():'Never'}</p><p>Credential: {a.token_type||'Type unavailable'} | Expires: {a.expires_at?new Date(a.expires_at*1000).toLocaleString():'No expiry reported'} | Data access expires: {a.data_access_expires_at?new Date(a.data_access_expires_at*1000).toLocaleString():'Not reported'}</p><p>Granted permissions: {a.permissions?.join(', ')||'Not inspected'}</p><div className="button-row">{!a.enabled&&a.token_status==='healthy'&&<button disabled={busy} onClick={()=>run(async()=>{await api(brandPath(brandId,`/accounts/${a.id}/activate`),'POST');await refresh()},'Account selected')}>Select account</button>}<button disabled={busy} onClick={()=>run(async()=>{await api(brandPath(brandId,`/accounts/${a.id}/check`),'POST');await refresh()},'Connection checked')}>Check connection</button><button disabled={busy} onClick={()=>{if(confirm('Remove this locally stored authorization?'))void run(async()=>{await api(brandPath(brandId,`/accounts/${a.id}`),'DELETE');await refresh()},'Authorization removed')}}>Disconnect</button></div></div>)}{!accounts?.length&&<p className="muted">No verified accounts yet. The owner-created social profiles are not connected until OAuth discovery succeeds.</p>}</Panel></div>
}
