import {useEffect,useState} from 'react'
import {Plus, Trash2, Sparkles, Paintbrush, Save, Download} from 'lucide-react'
import {api,apiUrl,brandPath,formats,kinds,label} from '../services/api'
import {readableError} from '../services/validation'
import {eligibleCandidates} from '../services/knowledgeCandidates'
import {useData} from '../hooks/useData'
import {Badge,Field,Panel,useAction,Status} from '../components/ui'
import type {Account,Brand,Content,Job,Knowledge,Publication,Slide} from '../types'

type Draft=Pick<Content,'topic'|'pillar'|'format'|'hook'|'body'|'slides'|'caption'|'cta'|'hashtags'|'knowledge_refs'|'sources'|'targets'|'parent_id'>
type DryRunResult={status:string;reasons:string[];plan_token:string|null;plan:{platform:string;account_name:string;caption:string;media_count:number;revision:number;action:string;graph_steps:{method:string;path:string;purpose:string}[]}[]}
const fresh=(brand:Brand):Draft=>({topic:'',pillar:brand.config.pillars[0]||'General',format:'carousel',hook:'',body:'',slides:[{title:'',body:'',kind:'cover',items:[]}],caption:'',cta:'',hashtags:[],knowledge_refs:[],sources:[],targets:brand.config.platforms,parent_id:null})
const normalized=(value:string)=>value.normalize('NFKC').replace(/\s+/g,' ').trim().toLocaleLowerCase()
export default function Editor({brand,id,navigate}: {brand:Brand;id?:number;navigate:(page:string,id?:number)=>void}) {
 const [draft,setDraft]=useState<Draft>(fresh(brand)),[item,setItem]=useState<Content|null>(null),[candidates,setCandidates]=useState<{key:string;items:Knowledge[];loading:boolean;error:string}>({key:'',items:[],loading:false,error:''}),[when,setWhen]=useState(''),[override,setOverride]=useState(false),[reason,setReason]=useState(''),[tab,setTab]=useState('Write'),[error,setError]=useState(''),[generationStatus,setGenerationStatus]=useState('')
 const [publishAccounts,setPublishAccounts]=useState<Account[]>([]),[selectedPlatforms,setSelectedPlatforms]=useState<string[]>([])
 const [dryRuns,setDryRuns]=useState<Record<string,DryRunResult>>({})
 const {busy,run}=useAction(),path=(suffix:string)=>brandPath(brand.id,suffix)
 const {data: controls} = useData<{global_paused:boolean;brand_paused:boolean}>(`/system?brand_id=${brand.id}`)
 const {data: receipts,reload: reloadReceipts} = useData<Publication[]>(path('/publications'))
 const candidateKey=JSON.stringify([brand.id,draft.topic,draft.pillar])
 const knowledge=candidates.key===candidateKey?eligibleCandidates(candidates.items,brand.id,draft.pillar):[]
 const editorialReview=item?.quality.editorial
 const provenance=item?.generation as unknown as {knowledge?:{id:number;title:string;source:string}[];ai_revised?:boolean}|undefined
 const exactCaption=item?[item.caption,item.cta,item.hashtags.map(h=>'#'+h.replace(/^#/, '')).join(' ')].filter(Boolean).join('\n\n'):''
 useEffect(()=>{let active=true;setDryRuns({});setSelectedPlatforms([]);if(id)api<Content>(path(`/content/${id}`)).then(c=>{if(active){setItem(c);setDraft({...Object.fromEntries(Object.keys(fresh(brand)).map(k=>[k,c[k as keyof Content]])),slides:c.slides.length?c.slides:fresh(brand).slides} as Draft)}}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[id,brand.id])
 useEffect(()=>{let active=true;api<Account[]>(path('/accounts')).then(rows=>{if(active)setPublishAccounts(rows)}).catch(()=>{});return()=>{active=false}},[brand.id])
 useEffect(()=>{let active=true;setCandidates({key:candidateKey,items:[],loading:!!draft.topic.trim()&&!!draft.pillar.trim(),error:''})
  if(!draft.topic.trim()||!draft.pillar.trim())return()=>{active=false}
  const timer=setTimeout(()=>{const query=new URLSearchParams({topic:draft.topic,pillar:draft.pillar})
   api<Knowledge[]>(path('/knowledge/candidates?'+query.toString())).then(items=>{if(active)setCandidates({key:candidateKey,items:eligibleCandidates(items,brand.id,draft.pillar),loading:false,error:''})})
    .catch(reason=>{if(active)setCandidates({key:candidateKey,items:[],loading:false,error:readableError(reason)})})},250)
  return()=>{active=false;clearTimeout(timer)}},[candidateKey])
 function field<K extends keyof Draft>(key:K,value:Draft[K]){setDryRuns({});setDraft(d=>({...d,[key]:value,...(key==='topic'||key==='pillar'?{knowledge_refs:[]}:{} )}))}
 function slide(index:number,patch:Partial<Slide>){field('slides',draft.slides.map((s,i)=>i===index?{...s,...patch}:s))}
 async function save(){setError('');setDryRuns({});try{const slides=draft.slides.filter((slide,index)=>!(index===0&&slide.kind==='cover'&&!slide.title.trim()&&!slide.body.trim()&&slide.items.every(value=>!value.trim())))
  const result=await api<Content>(path(id?`/content/${id}`:'/content'),id?'PUT':'POST',{...draft,slides});setItem(result);if(!id)navigate('Create',result.id);return result
 }catch(reason){const message=`Draft couldn't be saved: ${readableError(reason)}`;setError(message);throw new Error(message)}}
 async function generate(){setError('');setGenerationStatus('');try{
  if(!brand.enabled)throw new Error('Enable this brand before generating')
  if(draft.topic.trim().length<3)throw new Error('Enter a topic of at least 3 characters')
  if(!brand.config.pillars.some(pillar=>normalized(pillar)===normalized(draft.pillar)))throw new Error('Choose a configured pillar for this brand')
  if(draft.knowledge_refs.length>8)throw new Error('Select up to 8 knowledge records for generation')
  if(item&&(['topic','pillar','format','hook','body','slides','caption','cta','hashtags','sources','targets'] as const).some(key=>JSON.stringify(draft[key])!==JSON.stringify(item[key])))throw new Error('Save draft changes before generating so the local model receives the current draft')
  const query=new URLSearchParams({topic:draft.topic,pillar:draft.pillar})
  const current=await api<Knowledge[]>(path('/knowledge/candidates?'+query.toString()))
  const eligible=eligibleCandidates(current,brand.id,draft.pillar)
  if(!eligible.length)throw new Error('Approve and enable knowledge for this pillar before generating')
  if(!draft.knowledge_refs.length)throw new Error('Select and attach approved knowledge before generating')
  if(draft.knowledge_refs.some(ref=>!eligible.some(item=>item.id===ref)))throw new Error('Knowledge selection changed. Review the current candidates before generating')
  const queued=await api<Job>(path('/generate'),'POST',{topic:draft.topic,pillar:draft.pillar,format:draft.format,parent_id:item?.id||draft.parent_id,knowledge_refs:draft.knowledge_refs,update_current:false})
  setGenerationStatus('Generating with local AI…')
  for(let attempt=0;attempt<180;attempt++){
   await new Promise(resolve=>setTimeout(resolve,2000))
   const job=await api<Job>(path(`/jobs/${queued.id}`))
   if(job.status==='DONE'){
    const result=await api<Content>(path(`/content/${job.target_id}`))
    if(id===result.id){setItem(result);setDraft({...Object.fromEntries(Object.keys(fresh(brand)).map(key=>[key,result[key as keyof Content]])),slides:result.slides.length?result.slides:fresh(brand).slides} as Draft)}
    else navigate('Create',result.id)
    setGenerationStatus('')
    return
   }
   if(['FAILED','CANCELLED','UNKNOWN'].includes(job.status))throw new Error(`Local AI generation failed: ${job.error||job.status.toLowerCase()}`)
   if(job.status==='PENDING'&&job.error)setGenerationStatus(`Local AI retrying: ${job.error}`)
  }
  throw new Error('Local AI generation is still running. Check Jobs for its status.')
 }catch(reason){setGenerationStatus('');const message=readableError(reason);setError(message);throw new Error(message)}}
 async function action(name:string,body?:unknown){if(!id)throw new Error('Save your draft first');setDryRuns({});const result=await api<Content>(path(`/content/${id}/${name}`),'POST',body);if(result.topic)setItem(result)}
 return <><div className="toolbar"><div className="grow"><Badge>{item?.status||'NEW DRAFT'}</Badge>{item&&<span className="muted"> Revision {item.revision} · {item.status==='DRAFT'||item.quality.score==null?'Not scored yet':`Editorial quality ${item.quality.score}/100`}</span>}</div><button disabled={busy} onClick={()=>run(save)}><Save size={16}/>Save draft</button><button className="primary" disabled={busy||!draft.topic} onClick={()=>run(generate,'Content generated')}><Sparkles size={16}/>{id?'Generate new variant':'Generate with local AI'}</button></div><Status error={error}/>{generationStatus&&<p role="status" className="notice">{generationStatus}</p>}{error.startsWith('Local AI')&&<button className="text-button" onClick={()=>navigate('System')}>Open System diagnostics</button>}
 <div className="editor-grid"><div><Panel title="The idea"><div className="form-grid"><Field label="Topic"><input value={draft.topic} onChange={e=>field('topic',e.target.value)} placeholder="One useful thing your audience should know"/></Field><Field label="Pillar"><input list="pillars" value={draft.pillar} onChange={e=>field('pillar',e.target.value)}/><datalist id="pillars">{brand.config.pillars.map(p=><option key={p}>{p}</option>)}</datalist></Field><Field label="Format"><select value={draft.format} onChange={e=>field('format',e.target.value)}>{formats.map(f=><option key={f} value={f}>{label(f)}</option>)}</select></Field><Field label="Hook"><textarea rows={2} value={draft.hook} onChange={e=>field('hook',e.target.value)}/></Field></div></Panel>
 <Panel title="Make it useful" action={<div className="segmented">{['Write','Preview'].map(t=><button className={tab===t?'selected':''} key={t} onClick={()=>setTab(t)}>{t}</button>)}</div>}>
 {tab==='Write'?<><Field label="Body / script"><textarea rows={4} value={draft.body} onChange={e=>field('body',e.target.value)}/></Field>{draft.slides.map((s,i)=><div className="slide-editor" key={i}><div className="panel-heading"><strong>Slide {String(i+1).padStart(2,'0')}</strong><button className="icon-button" aria-label={`Remove slide ${i+1}`} onClick={()=>field('slides',draft.slides.filter((_,n)=>n!==i))}><Trash2 size={16}/></button></div><div className="form-grid"><Field label="Layout"><select value={s.kind} onChange={e=>slide(i,{kind:e.target.value})}>{kinds.map(k=><option key={k} value={k}>{label(k)}</option>)}</select></Field><Field label="Title"><input value={s.title} onChange={e=>slide(i,{title:e.target.value})}/></Field></div><Field label="Explanation"><textarea rows={3} value={s.body} onChange={e=>slide(i,{body:e.target.value})}/></Field><Field label="List items / column content" hint="One item per line. Two-column layouts need at least two items."><textarea value={s.items.join('\n')} onChange={e=>slide(i,{items:e.target.value.split('\n')})}/></Field><div className="button-row"><button disabled={i===0} onClick={()=>{const slides=[...draft.slides];[slides[i-1],slides[i]]=[slides[i],slides[i-1]];field('slides',slides)}}>Move up</button><button disabled={i===draft.slides.length-1} onClick={()=>{const slides=[...draft.slides];[slides[i+1],slides[i]]=[slides[i],slides[i+1]];field('slides',slides)}}>Move down</button></div></div>)}<button onClick={()=>field('slides',[...draft.slides,{title:'',body:'',kind:'statement',items:[]}])}><Plus size={16}/>Add slide</button></>:<div className="preview-grid">{item?.assets.length?item.assets.map((a,i)=><a key={a.key} href={apiUrl('/media/'+a.key)} target="_blank" rel="noreferrer"><img src={apiUrl('/media/'+a.key)} alt={`Rendered slide ${i+1}`}/></a>):<p>Save, then render your graphics to see a faithful preview.</p>}</div>}
 </Panel><Panel title="Caption & context"><Field label="Caption"><textarea rows={5} value={draft.caption} onChange={e=>field('caption',e.target.value)}/></Field><Field label="Call to action"><input value={draft.cta} onChange={e=>field('cta',e.target.value)}/></Field><Field label="Hashtags (space separated)"><input value={draft.hashtags.join(' ')} onChange={e=>field('hashtags',e.target.value.split(/\s+/).filter(Boolean))}/></Field><Field label="Source references (one per line)"><textarea value={draft.sources.join('\n')} onChange={e=>field('sources',e.target.value.split('\n').filter(Boolean))}/></Field></Panel></div>
 <aside><Panel title="Publishing checklist"><div className="stack"><button disabled={busy||!id} onClick={()=>run(async()=>{await save();await action('render');setTab('Preview')},'Graphics rendered')}><Paintbrush size={16}/>Save & render graphics</button>{item?.status==='DRAFT'&&<button disabled={busy} onClick={()=>run(()=>action('transition',{state:'REVIEW'}),'Sent to review')}>Send to review</button>}{item?.status==='REVIEW'&&<button disabled={busy} className="primary" onClick={()=>run(()=>action('transition',{state:'APPROVED'}),'Content approved')}>Approve content</button>}{item&&['APPROVED','SCHEDULED','FAILED'].includes(item.status)&&<button disabled={busy} onClick={()=>run(()=>action('transition',{state:item.status==='SCHEDULED'?'APPROVED':'DRAFT'}),'Returned for editing')}>{item.status==='SCHEDULED'?'Unschedule':'Return to draft'}</button>}<button disabled={busy||!id||!['APPROVED','SCHEDULED','PUBLISHED'].includes(item?.status||'')} onClick={()=>run(async()=>{const r=await api<{url:string}>(path(`/content/${id}/export`),'POST');window.location.assign(r.url)},'Export prepared')}><Download size={16}/>Manual export</button></div>{item?.quality.warnings?.map(w=><p className="notice" key={w}>{w}</p>)}{item?.quality.errors?.map(w=><p className="error" key={w}>{w}</p>)}</Panel>
 <Panel title="Grounded in knowledge"><p className="muted">Current topic and pillar: {draft.pillar}. Candidates refresh as you edit; Generate checks them again.</p>{candidates.key===candidateKey&&candidates.loading&&<p className="muted">Finding eligible knowledge…</p>}{candidates.key===candidateKey&&candidates.error&&<p role="alert" className="error">{candidates.error}</p>}<div className="check-list">{knowledge.map(k=><label key={k.id}><input type="checkbox" checked={draft.knowledge_refs.includes(k.id)} onChange={e=>field('knowledge_refs',e.target.checked?[...draft.knowledge_refs,k.id]:draft.knowledge_refs.filter(x=>x!==k.id))}/>{k.title}</label>)}</div>{!!knowledge.length&&<p className="muted">{draft.knowledge_refs.length} selected · {item?.knowledge_refs.length||0} attached to saved draft</p>}{draft.knowledge_refs.length>0&&<button disabled={busy} onClick={()=>run(save,'Knowledge attached to draft')}><Save size={16}/>Save selected knowledge</button>}{candidates.key===candidateKey&&!candidates.loading&&draft.knowledge_refs.some(ref=>!knowledge.some(k=>k.id===ref))&&<p className="notice">Some saved references are no longer current candidates. Review them before generating.</p>}{candidates.key===candidateKey&&!candidates.loading&&!knowledge.length&&<p className="muted">No approved, retrieval-enabled knowledge matches this pillar. Approve relevant knowledge before generating.</p>}{!draft.knowledge_refs.length&&!!knowledge.length&&<p className="notice">Select approved knowledge to ground generation. Nothing is attached yet.</p>}</Panel>
 <Panel title="Where & when"><div className="check-list">{['manual','instagram','facebook'].map(p=><label key={p}><input type="checkbox" checked={draft.targets.includes(p)} onChange={e=>field('targets',e.target.checked?[...draft.targets,p]:draft.targets.filter(x=>x!==p))}/>{label(p)}</label>)}</div><p className="muted">Save target changes before approval.</p><Field label="Publish time (your device timezone)"><input type="datetime-local" value={when} onChange={e=>setWhen(e.target.value)}/></Field><label className="check"><input type="checkbox" checked={override} onChange={e=>setOverride(e.target.checked)}/>Override brand posting window</label>{override&&<Field label="Override reason"><input value={reason} onChange={e=>setReason(e.target.value)}/></Field>}<button disabled={busy||!when||!id} onClick={()=>run(async()=>{await action('schedule',{run_at:new Date(when).toISOString(),override_window:override,reason});navigate('Calendar')},'Content scheduled')}>Schedule content</button></Panel>
 <Panel title="Manual Meta publishing dry run">
  <p className="muted">Choose each destination independently. The dry run checks approved content, credentials, permissions and public media without a Meta publishing request.</p>
  <div className="check-list">{(['instagram','facebook'] as const).map(platform=>{
   const account=publishAccounts.find(a=>a.platform===platform&&a.enabled)
   const receipt=receipts?.find(p=>p.content_id===item?.id&&p.platform===platform)
   return <label key={platform}><input type="checkbox" checked={selectedPlatforms.includes(platform)} onChange={e=>{setDryRuns({});setSelectedPlatforms(e.target.checked?[...selectedPlatforms,platform]:selectedPlatforms.filter(p=>p!==platform))}}/>
    {platform==='instagram'?'Instagram':'Facebook'}: {account?.name||account?.account_id||'No selected account'}
    {receipt?.state==='PUBLISHED'?' | PUBLISHED':account?.publishing_reason?' | '+account.publishing_reason:''}
   </label>
  })}</div>
  <p><strong>Brand:</strong> {brand.name} | <strong>Content:</strong> #{item?.id||'unsaved'}</p>
  <p><strong>Exact caption:</strong></p><pre className="json-view">{exactCaption||item?.body||'None'}</pre>
  <div className="preview-grid">{item?.assets.map((asset,index)=><img key={asset.key} src={apiUrl('/media/'+asset.key)} alt={`Exact rendered media ${index+1}`}/>)}</div>
  <button disabled={busy||!id||!selectedPlatforms.length||!['APPROVED','PUBLISHED'].includes(item?.status||'')} onClick={()=>run(async()=>{
   const results:Record<string,DryRunResult>={}
   for(const platform of selectedPlatforms){
    const account=publishAccounts.find(a=>a.platform===platform&&a.enabled)
    try{results[platform]=await api<DryRunResult>(path(`/content/${id}/publish-dry-run`),'POST',{platforms:[platform],account_ids:{[platform]:account?.id}})}
    catch(error){results[platform]={status:'BLOCKED',reasons:[readableError(error)],plan_token:null,plan:[]}}
   }
   setDryRuns(results)
  },'Destination dry runs finished')}>Run selected dry runs</button>
  {selectedPlatforms.map(platform=>{
   const result=dryRuns[platform]
   const receipt=receipts?.find(p=>p.content_id===item?.id&&p.platform===platform)
   const account=publishAccounts.find(a=>a.platform===platform&&a.enabled)
   const publishedAt=(receipt?.request_state as {published_at?:string}|undefined)?.published_at
   return <div key={platform} role="status">
    <h3>{platform==='facebook'?'Facebook':'Instagram'}: {result?.status||'Not checked'}</h3>
    {account?.publishing_reason&&<p className="notice">{account.publishing_reason}</p>}
    {result?.reasons.map(reason=><p className="error" key={reason}>{reason}</p>)}
    {result?.plan.map(plan=><div key={plan.platform}><p>{plan.action} | {plan.account_name}: {plan.media_count} images | {plan.caption}</p>{plan.graph_steps.length>0&&<p className="muted">Graph plan: {plan.graph_steps.map(step=>`${step.method} /${step.path} (${step.purpose})`).join(' | ')}</p>}</div>)}
    {receipt&&<p><strong>{receipt.state}</strong> | {platform==='facebook'?'Facebook':'Instagram'} | {account?.name||brand.name}{receipt.external_id&&<> | Provider publication ID: <code>{receipt.external_id}</code></>}{publishedAt&&<> | Published: {new Date(publishedAt).toLocaleString()}</>}</p>}
    {result?.status==='READY'&&<>
     <p className="notice">{brand.paused||controls?.brand_paused?'Brand outward actions must be active for a real manual publish.':controls?.global_paused?'Autonomous actions are paused. This owner-confirmed manual publish is still allowed.':'Brand outward actions are active for this owner-confirmed manual publish.'}</p>
     <button className="primary" disabled={busy||!item||brand.paused||controls?.brand_paused} onClick={()=>{
      if(window.confirm(`Final confirmation: publish content #${item?.id} for ${brand.name} to ${platform}?

${platform==='facebook'?(result.plan[0]?.caption||item?.body):exactCaption}`))void run(async()=>{
       const published=await api<Content>(path(`/content/${id}/manual-meta-publish`),'POST',{platforms:[platform],account_ids:{[platform]:account?.id},revision:item?.revision,exact_caption:exactCaption,media_keys:item?.assets.map(a=>a.key),plan_token:result.plan_token,confirmed:true})
       setItem(published);setDryRuns(previous=>{const next={...previous};delete next[platform];return next});await reloadReceipts()
      },`${platform==='facebook'?'Facebook':'Instagram'} manual publish completed`)
     }}>Final confirmation: publish to {platform==='facebook'?'Facebook':'Instagram'}</button>
    </>}
   </div>
  })}
 </Panel>
 {item&&<Panel title="Editorial review">{editorialReview?<><p className="muted">{editorialReview.note}</p><div className="stack">{Object.entries(editorialReview.dimensions).map(([name,part])=><div key={name}><strong>{label(name)}</strong> {part.score}/{part.max}</div>)}</div>{editorialReview.issues.map(issue=><p className="notice" key={issue}>{issue}</p>)}</>:<p className="muted">Save or generate content to see editorial signals.</p>}{provenance?.knowledge?.length?<><p><strong>Knowledge used</strong></p>{provenance.knowledge.map(ref=><p key={ref.id}>#{ref.id} {ref.title}{ref.source?` · ${ref.source}`:''}</p>)}</>:null}{provenance?.ai_revised!==undefined&&<p className="muted">AI revision pass: {provenance.ai_revised?'Used once':'Not needed'}</p>}</Panel>}
 {item&&<Panel title="Version & provenance"><p>Parent: {item.parent_id?<button className="text-button" onClick={()=>navigate('Create',item.parent_id!)}>Open original #{item.parent_id}</button>:'Original content'}</p><p className="muted">Generate new variant keeps this candidate intact for comparison.</p><details><summary>Generation metadata</summary><pre className="json-view">{JSON.stringify(item.generation,null,2)}</pre></details><button onClick={()=>run(()=>action('transition',{state:'ARCHIVED',reason:'Archived by administrator'}),'Content archived')} disabled={busy||item.status==='ARCHIVED'||item.status==='PUBLISHING'}>Archive content</button></Panel>}</aside></div></>
}
