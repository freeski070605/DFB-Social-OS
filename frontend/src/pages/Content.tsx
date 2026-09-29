import {useState} from 'react'
import {Search,Plus,Image as ImageIcon,ArrowUpRight} from 'lucide-react'
import {useData} from '../hooks/useData'
import {api,apiUrl,brandPath,date,label} from '../services/api'
import {Badge,Empty,Status,useAction} from '../components/ui'
import type {Brand,Content as ContentType} from '../types'

type Derivative={id:number;format:string;status:string;content_id:number|null;used_knowledge_refs:number[];output:Record<string,unknown>}
type Package={id:number;topic:string;pillar:string;audience_promise:string;angle:string;knowledge_refs:number[];derivatives:Derivative[]}
const formats=['INSTAGRAM_CAROUSEL','FACEBOOK_POST','INSTAGRAM_REEL','TIKTOK_VIDEO','YOUTUBE_SHORT','YOUTUBE_LONGFORM','THREADS_POST']

export default function Content({brand,navigate}:{brand:Brand;navigate:(page:string,id?:number)=>void}){
 const {data,error,loading}=useData<ContentType[]>(brandPath(brand.id,'/content'))
 const {data:packages,reload}=useData<Package[]>(brandPath(brand.id,'/packages'))
 const [query,setQuery]=useState(''),[status,setStatus]=useState('ALL'),[selected,setSelected]=useState<number|null>(null)
 const {busy,run}=useAction(),active=packages?.find(p=>p.id===selected)
 const items=(data||[]).filter(x=>(status==='ALL'||x.status===status)&&`${x.topic} ${x.pillar}`.toLowerCase().includes(query.toLowerCase()))
 async function attach(id:number){const item=await api<Package>(brandPath(brand.id,`/packages/from-content/${id}`),'POST');await reload();setSelected(item.id)}
 async function generate(format:string){if(!active)return;await api(brandPath(brand.id,`/packages/${active.id}/derivatives`),'POST',{format});await reload()}
 async function exportProduction(id:number){if(!active)return;const data=await api<Record<string,unknown>>(brandPath(brand.id,`/packages/${active.id}/derivatives/${id}/production`));const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=`production-${id}.json`;link.click();URL.revokeObjectURL(url)}
 return <><div className="toolbar"><div className="search"><Search size={18}/><input aria-label="Search content" placeholder="Find a topic, pillar, or idea..." value={query} onChange={e=>setQuery(e.target.value)}/></div><select aria-label="Filter by status" value={status} onChange={e=>setStatus(e.target.value)}>{['ALL','DRAFT','REVIEW','APPROVED','SCHEDULED','PUBLISHED','FAILED','ARCHIVED'].map(x=><option key={x}>{x}</option>)}</select><button className="primary" onClick={()=>navigate('Create')}><Plus size={17}/>New content</button></div><Status error={error} loading={loading}/>
 {!loading&&!items.length&&<Empty title="Useful content starts here" text="Build a knowledge-backed draft or write one yourself." action={<button onClick={()=>navigate('Create')}>Create your first piece</button>}/>}
 <div className="content-grid">{items.map(item=><div className="content-card" key={item.id}><button onClick={()=>navigate('Create',item.id)}><div className="content-art">{item.assets.length?<img src={apiUrl('/media/'+item.assets[0].preview)} alt={item.slides[0]?.title||item.topic}/>:<div><ImageIcon size={28}/><span>{item.hook||item.topic}</span><small>Not rendered yet</small></div>}<Badge>{item.status}</Badge></div><div className="content-card-body"><small>{label(item.format)} / {item.pillar}</small><h3>{item.topic}</h3><div><span>{date(item.scheduled_at)}</span><ArrowUpRight size={17}/></div></div></button>{!packages?.some(p=>p.derivatives.some(d=>d.content_id===item.id))&&<button disabled={busy||!item.knowledge_refs.length} onClick={()=>run(()=>attach(item.id),'Package created')}>Create package</button>}</div>)}</div>
 <section className="panel"><h2>Content packages</h2><p className="muted">One grounded idea with platform-native derivatives. Generate only the formats you choose.</p><div className="button-row">{packages?.map(p=><button key={p.id} onClick={()=>setSelected(p.id)} aria-pressed={selected===p.id}>#{p.id} {p.topic}</button>)}</div>{active&&<><h3>{active.topic}</h3><p>{active.pillar} / {active.audience_promise}</p><p>Approved grounding: {active.knowledge_refs.map(id=>`#${id}`).join(', ')}</p><div className="columns">{formats.map(format=>{const d=active.derivatives.find(item=>item.format===format);return <div className="account-row" key={format}><h3>{label(format)}</h3><Badge>{d?.status||'NOT CREATED'}</Badge>{d?<><p>Used knowledge: {d.used_knowledge_refs.map(id=>`#${id}`).join(', ')}</p>{format.includes('VIDEO')||format.includes('YOUTUBE')||format==='INSTAGRAM_REEL'?<button onClick={()=>run(()=>exportProduction(d.id),'Production JSON exported')}>Export production JSON</button>:null}<details><summary>Editorial output</summary><pre style={{whiteSpace:'pre-wrap'}}>{JSON.stringify(d.output,null,2)}</pre></details></>:<button disabled={busy} onClick={()=>run(()=>generate(format),'Derivative saved for review')}>Generate</button>}</div>})}</div></>}</section></>
}

