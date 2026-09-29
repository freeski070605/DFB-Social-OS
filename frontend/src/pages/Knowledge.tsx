import {useCallback,useEffect,useRef,useState} from 'react'
import {Plus,Search,BookOpen,Upload,Download} from 'lucide-react'
import {api,brandPath} from '../services/api'
import {Badge,Empty,Field,Modal,Panel,Status,useAction} from '../components/ui'
import {KnowledgeImport,KnowledgeExport} from './KnowledgeTransfer'
import type {Brand,Knowledge as Entry} from '../types'

type ReviewEntry = Entry & {version:string;safety_reasons:string[]}
type Facet = {name:string;count:number}
type ReviewPage = {total:number;page:number;page_size:number;rows:ReviewEntry[];categories:Facet[];sources:Facet[]}
type Selection = {selection_token:string;count:number;pillars:Record<string,number>;sources:Record<string,number>;restrictions_count:number;sensitive_count:number}
type Action = 'APPROVE'|'PENDING'|'REJECT'|'ENABLE'|'DISABLE'
type Filters = {q:string;verification:string;enabled:string;category:string;source:string;source_contains:string;tag:string;safety:string;sort:string}
const initialFilters:Filters={q:'',verification:'PENDING',enabled:'all',category:'',source:'',source_contains:'',tag:'',safety:'all',sort:'newest'}
const blank={category:'',title:'',body:'',source:'',verification:'PENDING',enabled:true,tags:[] as string[],restrictions:''}

export default function Knowledge({brand}:{brand:Brand}) {
 const [filters,setFilters]=useState<Filters>(initialFilters),[page,setPage]=useState(1)
 const [selected,setSelected]=useState<Record<number,string>>({}),[matching,setMatching]=useState<Selection|null>(null)
 const [confirm,setConfirm]=useState<{action:Action;selection:Selection}|null>(null),[includeSensitive,setIncludeSensitive]=useState(false)
 const [reviewMode,setReviewMode]=useState(false),[reviewIndex,setReviewIndex]=useState(0),[reviewed,setReviewed]=useState(0)
 const [actionMessage,setActionMessage]=useState('')
 const [editing,setEditing]=useState<(typeof blank & {id?:number})|null>(null)
 const [transfer,setTransfer]=useState<'import'|'export'|null>(null)
 const [loaded,setLoaded]=useState<{path:string;result:ReviewPage}|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true)
 const requestId=useRef(0)
 const {busy,run,toast}=useAction()
 const path=(suffix:string)=>brandPath(brand.id,suffix)
 const filterPayload={...filters,verification:filters.verification==='all'?null:filters.verification,
   enabled:filters.enabled==='all'?null:filters.enabled==='true'}
 const params=new URLSearchParams()
 Object.entries(filters).forEach(([key,value])=>{if(value&&value!=='all')params.set(key,value)})
 params.set('page',String(page))
 const reviewPath=path('/knowledge/review?'+params.toString())
 const data=loaded?.path===reviewPath?loaded.result:null
 const reload=useCallback(async()=>{
  const request=++requestId.current
  setLoading(true)
  try{const result=await api<ReviewPage>(reviewPath);if(request===requestId.current){setLoaded({path:reviewPath,result});setError('')}}
  catch(reason){if(request===requestId.current){setLoaded(null);setError((reason as Error).message)}}
  finally{if(request===requestId.current)setLoading(false)}
 },[reviewPath])
 useEffect(()=>{void reload();return()=>{requestId.current++}},[reload])
 const rows=data?.rows||[]
 const selectedCount=matching?.count||Object.keys(selected).length
 const current=rows[Math.min(reviewIndex,Math.max(0,rows.length-1))]
 useEffect(()=>{setFilters(initialFilters);setSelected({});setMatching(null);setConfirm(null);setPage(1);setReviewed(0);setReviewIndex(0);setActionMessage('')},[brand.id])
 useEffect(()=>{if(data&&page>Math.max(1,Math.ceil(data.total/data.page_size)))setPage(Math.max(1,Math.ceil(data.total/data.page_size)))},[data,page])
 function changeFilter(key:keyof Filters,value:string){setFilters(old=>({...old,[key]:value}));setPage(1);setReviewIndex(0);setSelected({});setMatching(null);setActionMessage('')}
 function clearSelection(){setSelected({});setMatching(null);setConfirm(null)}
 function selectVisible(){setMatching(null);setSelected(old=>({...old,...Object.fromEntries(rows.map(item=>[item.id,item.version]))}))}
 function selectRow(item:ReviewEntry,checked:boolean){setMatching(null);setSelected(old=>{const next={...old};if(checked)next[item.id]=item.version;else delete next[item.id];return next})}
 async function selectAllMatching(){try{const summary=await api<Selection>(path('/knowledge/review/selection'),'POST',{
   scope:'matching',filters:filterPayload});setMatching(summary);setSelected({});toast(`${summary.count} matching records selected`)}catch(reason){toast((reason as Error).message,true)}}
 async function selectionFor(item?:ReviewEntry){
  if(!item&&matching)return matching
  const ids=item?[{id:item.id,version:item.version}]:Object.entries(selected).map(([id,version])=>({id:Number(id),version}))
  return api<Selection>(path('/knowledge/review/selection'),'POST',{scope:'ids',ids})
 }
 async function prepare(action:Action,item?:ReviewEntry){try{const summary=await selectionFor(item);setIncludeSensitive(false);setConfirm({action,selection:summary})}
  catch(reason){toast((reason as Error).message,true)}}
 async function applyAction(action:Action,selection:Selection,include=false){
  const result=await api<{affected:number;skipped_sensitive:number}>(path('/knowledge/review/apply'),'POST',{
   selection_token:selection.selection_token,action,include_sensitive:include})
  clearSelection();await reload()
  setActionMessage(`${result.affected} records updated${result.skipped_sensitive?`; ${result.skipped_sensitive} safety-sensitive records skipped`:''}`)
  return result
 }
 async function singleAction(item:ReviewEntry,action:Action){
  if(action==='APPROVE'&&item.safety_reasons.length){await prepare(action,item);return}
  await run(async()=>{const summary=await selectionFor(item);await applyAction(action,summary,true);setReviewed(count=>count+1)},'Review action complete')
 }
 function nextReview(){if(reviewIndex+1<rows.length)setReviewIndex(reviewIndex+1);else if(data&&page*data.page_size<data.total){setPage(page+1);setReviewIndex(0)}}
 const update=(key:string,value:unknown)=>setEditing(old=>old?{...old,[key]:value}:old)
 const actionButtons: {action:Action;label:string}[]=[{action:'APPROVE',label:'Approve selected'},
  {action:'PENDING',label:'Mark pending'},{action:'REJECT',label:'Reject selected'},
  {action:'ENABLE',label:'Enable retrieval'},{action:'DISABLE',label:'Disable retrieval'}]
 return <>
 <div className="toolbar">
  <div className="search"><Search size={18}/><input aria-label="Search knowledge" placeholder="Search knowledge…" value={filters.q} onChange={event=>changeFilter('q',event.target.value)}/></div>
  <button className="primary" onClick={()=>setEditing({...blank,category:brand.config.pillars[0]||'General'})}><Plus size={16}/>Add knowledge</button>
  <button onClick={()=>setTransfer('import')}><Upload size={16}/>Import knowledge</button>
  <button onClick={()=>setTransfer('export')}><Download size={16}/>Export knowledge</button>
 </div>
 <div className="info-banner"><BookOpen size={20}/><p>Your source of truth. Only approved, enabled entries inform AI generation. Keep sources and usage restrictions attached.</p></div>
 <Panel title="Review filters"><div className="review-filter-grid">
  <Field label="Verification"><select value={filters.verification} onChange={e=>changeFilter('verification',e.target.value)}><option value="all">All states</option><option>PENDING</option><option>APPROVED</option><option>REJECTED</option></select></Field>
  <Field label="Retrieval"><select value={filters.enabled} onChange={e=>changeFilter('enabled',e.target.value)}><option value="all">Enabled and disabled</option><option value="true">Enabled</option><option value="false">Disabled</option></select></Field>
  <Field label="Pillar"><select value={filters.category} onChange={e=>changeFilter('category',e.target.value)}><option value="">All pillars</option>{data?.categories.map(f=><option key={f.name} value={f.name}>{f.name} ({f.count})</option>)}</select></Field>
  <Field label="Source"><select value={filters.source} onChange={e=>changeFilter('source',e.target.value)}><option value="">All sources</option>{data?.sources.map(f=><option key={f.name} value={f.name}>{f.name} ({f.count})</option>)}</select></Field>
  <Field label="Source contains"><input value={filters.source_contains} placeholder="CFPB" onChange={e=>changeFilter('source_contains',e.target.value)}/></Field>
  <Field label="Tag contains"><input value={filters.tag} onChange={e=>changeFilter('tag',e.target.value)}/></Field>
  <Field label="Safety"><select value={filters.safety} onChange={e=>changeFilter('safety',e.target.value)}><option value="all">All records</option><option value="sensitive">Safety-sensitive</option><option value="standard">Standard review</option></select></Field>
  <Field label="Sort"><select value={filters.sort} onChange={e=>changeFilter('sort',e.target.value)}><option value="newest">Recently updated</option><option value="oldest">Oldest first</option><option value="title">Title</option><option value="category">Pillar</option><option value="source">Source</option></select></Field>
 </div></Panel>
 <Status error={error} loading={loading}/>
 {actionMessage&&<p className="notice" role="status">{actionMessage}</p>}
 <div className="review-toolbar"><strong>{data?.total||0} matching · {selectedCount} records selected</strong>
  <button disabled={loading||!rows.length} onClick={selectVisible}>Select visible ({rows.length})</button>
  <button disabled={loading||!data?.total} onClick={()=>void selectAllMatching()}>Select all matching current filters</button>
  <button disabled={!selectedCount} onClick={clearSelection}>Clear selection</button>
  <button onClick={()=>{setReviewMode(!reviewMode);setReviewIndex(0)}}>{reviewMode?'Exit review mode':'Focused review mode'}</button>
 </div>
 {!!selectedCount&&<div className="review-actions">{actionButtons.map(button=><button key={button.action} disabled={busy} className={button.action==='APPROVE'?'primary':''} onClick={()=>void prepare(button.action)}>{button.label}</button>)}</div>}
 {reviewMode&&current?<Panel className="review-detail"><div className="panel-heading"><span className="eyebrow">Review mode · {Math.min((page-1)*(data?.page_size||30)+reviewIndex+1,data?.total||0)} of {data?.total}</span><strong>Reviewed {reviewed} of {data?.total}</strong></div>
  <div className="panel-heading"><h2>{current.title}</h2><Badge>{current.verification}</Badge></div>
  <p className="eyebrow">{current.category} · {current.enabled?'Retrieval enabled':'Retrieval disabled'}</p>
  {!!current.safety_reasons.length&&<p className="review-sensitive">Safety-sensitive: {current.safety_reasons.join(', ')}</p>}
  <div className="review-body">{current.body}</div>
  <p><strong>Source / reference</strong><br/>{current.source||'No source supplied'}</p>
  <p><strong>Usage restrictions</strong><br/>{current.restrictions||'None supplied'}</p>
  <p><strong>Tags</strong><br/>{current.tags.join(', ')||'None'}</p>
  <div className="review-actions"><button className="primary" onClick={()=>void singleAction(current,'APPROVE')}>Approve</button><button onClick={()=>void singleAction(current,'PENDING')}>Keep pending</button><button onClick={()=>setEditing(current)}>Edit</button><button onClick={()=>void singleAction(current,'DISABLE')}>Disable retrieval</button><button onClick={nextReview}>Next →</button></div>
 </Panel>:null}
 {!reviewMode&&<div className="knowledge-grid">{rows.map(item=><Panel key={item.id} className="knowledge-review-card">
  <div className="panel-heading"><label className="check"><input type="checkbox" aria-label={`Select ${item.title}`} checked={!!matching||!!selected[item.id]} disabled={!!matching} onChange={e=>selectRow(item,e.target.checked)}/><Badge>{item.verification}</Badge></label><small>{item.enabled?'Retrieval enabled':'Retrieval disabled'}</small></div>
  <small className="eyebrow">{item.category}</small><h3>{item.title}</h3><p className="clamp">{item.body}</p>
  {!!item.safety_reasons.length&&<p className="review-sensitive">Safety-sensitive: {item.safety_reasons.join(', ')}</p>}
  <p className="review-source"><strong>Source:</strong> {item.source||'No source supplied'}</p>
  {!!item.restrictions&&<p className="review-restrictions"><strong>Restrictions:</strong> {item.restrictions}</p>}
  <div className="tag-row">{item.tags.map(tag=><span key={tag}>{tag}</span>)}</div><button onClick={()=>setEditing(item)}>Review & edit</button>
 </Panel>)}</div>}
 {!loading&&!data?.total&&<Empty title="No knowledge matches these filters" text="Adjust the filters or add a knowledge record."/>}
 {!!data?.total&&<div className="review-toolbar"><button disabled={page<=1} onClick={()=>{setPage(page-1);setReviewIndex(0)}}>Previous page</button><span>Page {page} of {Math.ceil(data.total/data.page_size)}</span><button disabled={page*data.page_size>=data.total} onClick={()=>{setPage(page+1);setReviewIndex(0)}}>Next page</button></div>}
 {confirm&&<Modal title={`${confirm.action==='APPROVE'?'Approve':'Update'} knowledge`} close={()=>setConfirm(null)}><div className="review-confirm">
  <p><strong>{confirm.selection.count} records selected</strong></p>
  <p><strong>Pillars:</strong> {Object.entries(confirm.selection.pillars).map(([name,count])=>`${name} (${count})`).join(', ')}</p>
  <p><strong>Sources:</strong> {Object.entries(confirm.selection.sources).slice(0,15).map(([name,count])=>`${name} (${count})`).join(', ')}{Object.keys(confirm.selection.sources).length>15?` · ${Object.keys(confirm.selection.sources).length-15} more sources`:''}</p>
  <p><strong>Usage restrictions:</strong> {confirm.selection.restrictions_count} records</p>
  <p><strong>Safety-sensitive:</strong> {confirm.selection.sensitive_count} records</p>
  {confirm.action==='APPROVE'&&confirm.selection.sensitive_count>0&&<><p className="review-sensitive">Safety-sensitive records are excluded from bulk approval by default.</p><label className="check"><input type="checkbox" checked={includeSensitive} onChange={e=>setIncludeSensitive(e.target.checked)}/>Include safety-sensitive records</label></>}
  <div className="review-actions"><button onClick={()=>setConfirm(null)}>Cancel</button><button className="primary" disabled={busy} onClick={()=>void run(async()=>{const result=await applyAction(confirm.action,confirm.selection,includeSensitive);setReviewed(count=>count+result.affected);setConfirm(null)},'Review action complete')}>Confirm {confirm.action.toLowerCase()}</button></div>
 </div></Modal>}
 {editing&&<Modal title={editing.id?'Edit knowledge':'Add knowledge'} close={()=>setEditing(null)}><form onSubmit={event=>{event.preventDefault();void run(async()=>{const payload=Object.fromEntries(Object.keys(blank).map(key=>[key,editing[key as keyof typeof editing]]));await api(path(editing.id?`/knowledge/${editing.id}`:'/knowledge'),editing.id?'PUT':'POST',payload);setEditing(null);clearSelection();await reload()},'Knowledge saved')}}>
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
