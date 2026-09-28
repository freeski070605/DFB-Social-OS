import {useState} from 'react'
import {Search, Plus, Image as ImageIcon, ArrowUpRight} from 'lucide-react'
import {useData} from '../hooks/useData'
import {brandPath, date, label} from '../services/api'
import {Badge, Empty, Status} from '../components/ui'
import type {Brand, Content as ContentType} from '../types'
export default function Content({brand,navigate}: {brand: Brand;navigate: (page:string,id?:number)=>void}) {
 const {data,error,loading}=useData<ContentType[]>(brandPath(brand.id,'/content')), [query,setQuery]=useState(''),[status,setStatus]=useState('ALL')
 const items=(data||[]).filter(x=>(status==='ALL'||x.status===status)&&`${x.topic} ${x.pillar}`.toLowerCase().includes(query.toLowerCase()))
 return <><div className="toolbar"><div className="search"><Search size={18}/><input aria-label="Search content" placeholder="Find a topic, pillar, or idea…" value={query} onChange={e=>setQuery(e.target.value)}/></div><select aria-label="Filter by status" value={status} onChange={e=>setStatus(e.target.value)}>{['ALL','DRAFT','REVIEW','APPROVED','SCHEDULED','PUBLISHED','FAILED','ARCHIVED'].map(x=><option key={x}>{x}</option>)}</select><button className="primary" onClick={()=>navigate('Create')}><Plus size={17}/>New content</button></div><Status error={error} loading={loading}/>
 {!loading&&!items.length&&<Empty title="Useful content starts here" text="Build a knowledge-backed draft or write one yourself. Every format starts with structured content." action={<button onClick={()=>navigate('Create')}>Create your first piece</button>}/>}
 <div className="content-grid">{items.map(item=><button className="content-card" key={item.id} onClick={()=>navigate('Create',item.id)}><div className="content-art">{item.assets.length?<img src={'/api/media/'+item.assets[0].preview} alt={item.slides[0]?.title||item.topic}/>:<div><ImageIcon size={28}/><span>{item.hook||item.topic}</span><small>Not rendered yet</small></div>}<Badge>{item.status}</Badge></div><div className="content-card-body"><small>{label(item.format)} · {item.pillar}</small><h3>{item.topic}</h3><div><span>{date(item.scheduled_at)}</span><ArrowUpRight size={17}/></div></div></button>)}</div></>
}
