import {createContext, useContext, useState, type ReactNode} from 'react'
import {ArrowUpRight, LoaderCircle, X} from 'lucide-react'
export const ToastContext = createContext<(message: string, error?: boolean) => void>(() => {})
export function useAction() {
  const toast = useContext(ToastContext), [busy, setBusy] = useState(false)
  async function run(task: () => Promise<unknown>, message = 'Saved') { setBusy(true); try {await task(); toast(message)} catch(e) {toast((e as Error).message, true)} finally {setBusy(false)} }
  return {busy, run, toast}
}
export function Badge({children}: {children: ReactNode}) {const text = String(children); return <span className={'badge ' + text.toLowerCase()}>{text.replaceAll('_',' ')}</span>}
export function Empty({title, text, action}: {title: string; text: string; action?: ReactNode}) {return <div className="empty"><span className="empty-icon">✳</span><h3>{title}</h3><p>{text}</p>{action}</div>}
export function Status({error, loading}: {error?: string; loading?: boolean}) {return error ? <div role="alert" className="error">{error}</div> : loading ? <div className="loading"><LoaderCircle className="spin" size={18}/> Loading your workspace…</div> : null}
export function Panel({title, children, action, className = ''}: {title?: string; children: ReactNode; action?: ReactNode; className?: string}) {return <section className={'panel ' + className}>{title && <div className="panel-heading"><h2>{title}</h2>{action}</div>}{children}</section>}
export function Field({label, children, hint}: {label: string; children: ReactNode; hint?: string}) {return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>}
export function Modal({title, children, close}: {title: string; children: ReactNode; close: () => void}) {return <div className="modal-shade" onMouseDown={e => {if(e.target === e.currentTarget) close()}}><section className="modal" role="dialog" aria-modal="true" aria-label={title}><div className="panel-heading"><h2>{title}</h2><button className="icon-button" aria-label="Close dialog" onClick={close}><X size={20}/></button></div>{children}</section></div>}
export function LinkButton({children, onClick}: {children: ReactNode; onClick: () => void}) {return <button className="text-button" onClick={onClick}>{children}<ArrowUpRight size={15}/></button>}
export function JsonView({value}: {value: unknown}) {return <pre className="json-view">{JSON.stringify(value,null,2)}</pre>}
