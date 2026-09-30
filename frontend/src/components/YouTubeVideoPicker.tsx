import {useRef,useState,type ChangeEvent} from 'react'
import {readableError} from '../services/validation'

type Props={disabledReason:string|null;onRegister:(file:File)=>Promise<void>}

export function YouTubeVideoPicker({disabledReason,onRegister}:Props){
 const inputRef=useRef<HTMLInputElement>(null)
 const [file,setFile]=useState<File|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
 function selectFile(event:ChangeEvent<HTMLInputElement>){
  const selected=event.currentTarget.files?.[0]||null
  event.currentTarget.value=''
  if(selected){setFile(selected);setError('')}
 }
 async function registerFile(){
  if(!file||disabledReason||busy)return
  setBusy(true);setError('')
  try{await onRegister(file);setFile(null)}
  catch(reason){setError(readableError(reason))}
  finally{setBusy(false)}
 }
 return <div className="field">
  <span>Finished local video</span>
  <input ref={inputRef} id="youtube-video-file" aria-label="Local video file" type="file" accept="video/*" hidden onChange={selectFile}/>
  <button type="button" onClick={()=>inputRef.current?.click()}>Choose file</button>
  {file&&<div role="status"><p><strong>{file.name}</strong></p><p>{file.size.toLocaleString()} bytes ({(file.size/1024/1024).toFixed(1)} MB)</p><p>Selected locally; not uploaded or registered.</p></div>}
  {file&&<button type="button" disabled={!!disabledReason||busy} onClick={()=>void registerFile()}>{busy?'Registering video…':'Upload / register video'}</button>}
  {disabledReason&&<p className="muted">{disabledReason}</p>}
  {error&&<p role="alert" className="error">Video registration failed: {error}</p>}
 </div>
}