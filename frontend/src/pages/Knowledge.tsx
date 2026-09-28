import {useState} from 'react'
import {Plus,Search,BookOpen,Upload,Download} from 'lucide-react'
import {useData} from '../hooks/useData'
import {api,brandPath} from '../services/api'
import {Badge,Empty,Field,Modal,Panel,Status,useAction} from '../components/ui'
import {KnowledgeImport,KnowledgeExport} from './KnowledgeTransfer'
import type {Brand,Knowledge as Entry} from '../types'

const blank={category:'',title:'',body:'',source:'',verification:'PENDING',enabled:true,tags:[] as string[],restrictions:''}

export default function Knowledge({brand}:{brand:Brand}) {
 const [q,setQ]=useState('')
 const {data,error,loading,reload}=useData<Entry[]>(brandPath(brand.id,'/knowledge?q='+encodeURIComponent(q)))
 const [editing,setEditing]=useState<(typeof blank & {id?:number})|null>(null)
 const [transfer,setTransfer]=useState<'import'|'export'|null>(null)
 const {busy,run}=useAction()
 const update=(key:string,value:unknown)=>setEditing(current=>current?{...current,[key]:value}:current)

 return <><div className="toolbar">
  <div className="search"><Search size={18}/><input aria-label="Search knowledge" placeholder="Search your trusted knowledge…" value={q} onChange={event=>setQ(event.target.value)}/></div>
  <button className="primary" onClick={()=>setEditing({...blank,category:brand.config.pillars[0]||'General'})}><Plus size={16}/>Add knowledge</button>
  <button onClick={()=>setTransfer('import')}><Upload size={16}/>Import knowledge</button>
  <button onClick={()=>setTransfer('export')}><Download size={16}/>Export knowledge</button>
 </div>
 <div className="info-banner"><BookOpen size={20}/><p>Your source of truth. Only approved, enabled entries inform AI generation. Keep sources and usage restrictions attached.</p></div>
 <Status error={error} loading={loading}/>
 <div className="knowledge-grid">{data?.map(item=><Panel key={item.id}><div className="panel-heading"><Badge>{item.verification}</Badge><small>{item.enabled?'Enabled':'Disabled'}</small></div><small className="eyebrow">{item.category}</small><h3>{item.title}</h3><p className="clamp">{item.body}</p><div className="tag-row">{item.tags.map(tag=><span key={tag}>{tag}</span>)}</div><button onClick={()=>setEditing(item)}>Review & edit</button></Panel>)}</div>
 {!loading&&!data?.length&&<Empty title="Build your brand’s source of truth" text="Add practical guidance, a source, and any restrictions. Review it before making it available to your local AI."/>}
 {editing&&<Modal title={editing.id?'Edit knowledge':'Add knowledge'} close={()=>setEditing(null)}><form onSubmit={event=>{event.preventDefault();void run(async()=>{const payload=Object.fromEntries(Object.keys(blank).map(key=>[key,editing[key as keyof typeof editing]]));await api(brandPath(brand.id,editing.id?`/knowledge/${editing.id}`:'/knowledge'),editing.id?'PUT':'POST',payload);setEditing(null);await reload()},'Knowledge saved')}}>
  <Field label="Title"><input required value={editing.title} onChange={event=>update('title',event.target.value)}/></Field>
  <Field label="Category / pillar"><input required value={editing.category} onChange={event=>update('category',event.target.value)}/></Field>
  <Field label="Knowledge body"><textarea required rows={8} value={editing.body} onChange={event=>update('body',event.target.value)}/></Field>
  <Field label="Source / reference"><input value={editing.source} onChange={event=>update('source',event.target.value)}/></Field>
  <Field label="Usage restrictions"><textarea value={editing.restrictions} onChange={event=>update('restrictions',event.target.value)}/></Field>
  <Field label="Tags (comma separated)"><input value={editing.tags.join(', ')} onChange={event=>update('tags',event.target.value.split(',').map(tag=>tag.trim()))}/></Field>
  <Field label="Verification"><select value={editing.verification} onChange={event=>update('verification',event.target.value)}>{['PENDING','APPROVED','REJECTED'].map(state=><option key={state}>{state}</option>)}</select></Field>
  <label className="check"><input type="checkbox" checked={editing.enabled} onChange={event=>update('enabled',event.target.checked)}/>Available for retrieval</label>
  <button className="primary" disabled={busy}>Save knowledge</button>
 </form></Modal>}
 {transfer==='import'&&<KnowledgeImport brandId={brand.id} close={()=>setTransfer(null)} committed={reload}/>}
 {transfer==='export'&&<KnowledgeExport brandId={brand.id} close={()=>setTransfer(null)}/>}
 </>
}
