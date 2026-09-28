import {useState} from 'react'
import {api,apiUpload,brandPath} from '../services/api'
import {Field,Modal} from '../components/ui'

type Format = 'csv' | 'json'
type Mode = 'SKIP_DUPLICATES' | 'UPDATE_MATCHING'
type RecordData = {title:string;category:string;body:string;source:string;usage_restrictions:string;tags:string[];verification:string;available_for_retrieval:boolean}
type PreviewRow = {row:number;status:'VALID'|'INVALID'|'DUPLICATE';matching_id:number|null;duplicate_of_row:number|null;errors:string[];warnings:string[];record:RecordData|null}
type Preview = {total:number;valid:number;invalid:number;duplicates:number;warnings:number;rows:PreviewRow[];file_errors:string[];preview_token:string|null}
type Result = {total:number;created:number;updated:number;skipped:number;rejected:number}

export function KnowledgeImport({brandId,close,committed}:{brandId:number;close:()=>void;committed:()=>Promise<unknown>}) {
 const [file,setFile]=useState<File|null>(null),[format,setFormat]=useState<Format>('csv'),[content,setContent]=useState('')
 const [preview,setPreview]=useState<Preview|null>(null),[result,setResult]=useState<Result|null>(null)
 const [mode,setMode]=useState<Mode>('SKIP_DUPLICATES'),[skipInvalid,setSkipInvalid]=useState(false)
 const [filter,setFilter]=useState('All'),[page,setPage]=useState(0),[busy,setBusy]=useState(false),[error,setError]=useState('')
 const rows=(preview?.rows||[]).filter(row=>filter==='All'||(filter==='Issues'?(row.errors.length||row.warnings.length):row.status==='DUPLICATE'))
 const pages=Math.max(1,Math.ceil(rows.length/50)),shown=rows.slice(page*50,(page+1)*50)
 async function validate(){
  if(!file)return
  setBusy(true);setError('');setResult(null);setPreview(null)
  try{
   if(file.size>20_000_000)throw new Error('Import exceeds 20 MB; split it into smaller files')
   let text:string
   try{text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer())}
   catch{throw new Error('File must be UTF-8 encoded CSV or JSON')}
   const upload=new FormData()
   upload.append('format',format)
   upload.append('file',file)
   const result=await apiUpload<Preview>(brandPath(brandId,'/knowledge/import/preview'),upload)
   setContent(text);setPreview(result);setPage(0);setFilter('All');setSkipInvalid(false)
  }catch(reason){setError((reason as Error).message)}finally{setBusy(false)}
 }
 async function commit(){
  if(!preview?.preview_token)return
  setBusy(true);setError('')
  try{
   const summary=await api<Result>(brandPath(brandId,'/knowledge/import/commit'),'POST',{
    format,content,preview_token:preview.preview_token,duplicate_mode:mode,skip_invalid:skipInvalid})
   setResult(summary);await committed()
  }catch(reason){setError((reason as Error).message)}finally{setBusy(false)}
 }
 return <div className="knowledge-transfer-scope"><Modal title="Import knowledge" close={close}><div className="import-workflow">
  {!result?<>
   <p className="muted">Upload → Validate → Preview → Resolve → Commit. Nothing is added to knowledge during preview.</p>
   <Field label="CSV or JSON file"><input type="file" accept=".csv,.json,text/csv,application/json" onChange={event=>{const next=event.target.files?.[0]||null;setFile(next);setFormat(next?.name.toLowerCase().endsWith('.json')?'json':'csv');setPreview(null);setContent('');setError('')}}/></Field>
   <p><a href={`/api${brandPath(brandId,'/knowledge/import/template.csv')}`} download>Download CSV template</a></p>
   {file&&<p className="muted">{file.name} · {(file.size/1024).toFixed(1)} KB · {format.toUpperCase()}</p>}
   <button type="button" className="primary" disabled={!file||busy} onClick={()=>void validate()}>{busy?'Validating…':'Validate and preview'}</button>
   {error&&<p role="alert" className="error">{error}</p>}
   {preview&&<>
    <div className="import-counts"><strong>Total {preview.total}</strong><span>Valid {preview.valid}</span><span>Invalid {preview.invalid}</span><span>Duplicates {preview.duplicates}</span><span>Warnings {preview.warnings}</span></div>
    {preview.file_errors.map(message=><p role="alert" className="error" key={message}>{message}</p>)}
    {!!preview.rows.length&&<><div className="button-row"><Field label="Preview rows"><select value={filter} onChange={event=>{setFilter(event.target.value);setPage(0)}}><option>All</option><option>Issues</option><option>Duplicates</option></select></Field><span className="muted">Showing {rows.length} rows</span></div>
     <div className="import-preview-list">{shown.map(row=><div className="account-row" key={row.row}><strong>Row {row.row} · {row.status} · {row.record?.title||'Untitled'}</strong><p className="muted">{row.record?.category||'No category'}{row.matching_id?` · Matches #${row.matching_id}`:''}{row.duplicate_of_row?` · Matches row ${row.duplicate_of_row}`:''}</p>{row.errors.map(message=><p className="error" key={message}>{message}</p>)}{row.warnings.map(message=><p className="notice" key={message}>{message}</p>)}{row.record&&<details><summary>Inspect record</summary><p><strong>Verification:</strong> {row.record.verification} · <strong>Retrieval:</strong> {row.record.available_for_retrieval?'Enabled':'Disabled'}</p><p><strong>Source:</strong> {row.record.source||'—'}</p><p><strong>Tags:</strong> {row.record.tags.join(', ')||'—'}</p><p><strong>Restrictions:</strong> {row.record.usage_restrictions||'—'}</p><pre className="import-body">{row.record.body}</pre></details>}</div>)}</div>
     {pages>1&&<div className="button-row"><button disabled={page===0} onClick={()=>setPage(page-1)}>Previous</button><span>Page {page+1} of {pages}</span><button disabled={page>=pages-1} onClick={()=>setPage(page+1)}>Next</button></div>}</>}
    {preview.preview_token&&<><h3>Resolve duplicates</h3><label className="check"><input type="radio" name="duplicate-mode" checked={mode==='SKIP_DUPLICATES'} onChange={()=>setMode('SKIP_DUPLICATES')}/>Skip duplicates</label><label className="check"><input type="radio" name="duplicate-mode" checked={mode==='UPDATE_MATCHING'} onChange={()=>setMode('UPDATE_MATCHING')}/>Update matching existing records</label><p className="muted">Repeated rows within this file are always skipped. Updates affect only the matching record in this brand.</p>
     {preview.invalid>0&&<label className="check"><input type="checkbox" checked={skipInvalid} onChange={event=>setSkipInvalid(event.target.checked)}/>I reviewed {preview.invalid} invalid records; reject them and commit valid rows only</label>}
     <button className="primary" disabled={busy||preview.valid===0||(preview.invalid>0&&!skipInvalid)} onClick={()=>void commit()}>{busy?'Committing…':'Commit import'}</button></>}
   </>}
  </>:<><h3>Import complete</h3><div className="import-counts"><span>Created {result.created}</span><span>Updated {result.updated}</span><span>Skipped {result.skipped}</span><span>Rejected {result.rejected}</span></div><button className="primary" onClick={close}>Done</button></>}
 </div></Modal></div>
}

export function KnowledgeExport({brandId,close}:{brandId:number;close:()=>void}) {
 return <div className="knowledge-transfer-scope"><Modal title="Export knowledge" close={close}><p>Download every knowledge record for this brand, including sources, tags, restrictions, verification, and retrieval state.</p><div className="button-row"><a className="button-link" href={`/api${brandPath(brandId,'/knowledge/export.csv')}`} download>Download CSV</a><a className="button-link" href={`/api${brandPath(brandId,'/knowledge/export.json')}`} download>Download JSON</a></div></Modal></div>
}
