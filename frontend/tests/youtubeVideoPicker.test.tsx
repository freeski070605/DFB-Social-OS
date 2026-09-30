// @vitest-environment jsdom
import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,describe,expect,it,vi} from 'vitest'
import {YouTubeVideoPicker} from '../src/components/YouTubeVideoPicker'

afterEach(()=>{cleanup();vi.restoreAllMocks()})

function videoFile(){return new File(['video bytes'],'finished-video.mov',{type:'video/quicktime'})}

describe('YouTube video picker',()=>{
 it('opens the native input from an enabled Choose file button',()=>{
  const onRegister=vi.fn(async()=>{})
  render(<YouTubeVideoPicker disabledReason={null} onRegister={onRegister}/>)
  const input=screen.getByLabelText('Local video file') as HTMLInputElement
  const click=vi.spyOn(input,'click').mockImplementation(()=>{})
  expect(input.accept).toBe('video/*')
  expect(input.disabled).toBe(false)
  fireEvent.click(screen.getByRole('button',{name:'Choose file'}))
  expect(click).toHaveBeenCalledOnce()
  expect(onRegister).not.toHaveBeenCalled()
 })

 it('shows filename, size, and local-only state without registering on selection',()=>{
  const onRegister=vi.fn(async()=>{})
  render(<YouTubeVideoPicker disabledReason={null} onRegister={onRegister}/>)
  const input=screen.getByLabelText('Local video file') as HTMLInputElement
  fireEvent.change(input,{target:{files:[videoFile()]}})
  expect(screen.getByText('finished-video.mov')).toBeTruthy()
  expect(screen.getByText(/11 bytes/)).toBeTruthy()
  expect(screen.getByText('Selected locally; not uploaded or registered.')).toBeTruthy()
  expect(onRegister).not.toHaveBeenCalled()
 })

 it('treats picker cancellation as harmless',()=>{
  render(<YouTubeVideoPicker disabledReason={null} onRegister={vi.fn(async()=>{})}/>)
  const input=screen.getByLabelText('Local video file') as HTMLInputElement
  fireEvent.change(input,{target:{files:[]}})
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText('Selected locally; not uploaded or registered.')).toBeNull()
 })

 it('clears the native input value so the same file can be selected again',()=>{
  render(<YouTubeVideoPicker disabledReason={null} onRegister={vi.fn(async()=>{})}/>)
  const input=screen.getByLabelText('Local video file') as HTMLInputElement
  const file=videoFile()
  fireEvent.change(input,{target:{files:[file]}})
  expect(input.value).toBe('')
  fireEvent.change(input,{target:{files:[file]}})
  expect(input.value).toBe('')
  expect(screen.getByText('finished-video.mov')).toBeTruthy()
 })

 it('shows backend rejection after explicit local asset registration',async()=>{
  const onRegister=vi.fn(async()=>{throw new Error('Unsupported video container')})
  render(<YouTubeVideoPicker disabledReason={null} onRegister={onRegister}/>)
  fireEvent.change(screen.getByLabelText('Local video file'),{target:{files:[videoFile()]}})
  fireEvent.click(screen.getByRole('button',{name:'Upload / register video'}))
  const alert=await screen.findByRole('alert')
  expect(alert.textContent).toContain('Video registration failed: Unsupported video container')
  expect(onRegister).toHaveBeenCalledOnce()
 })

 it('does not submit a surrounding form when Choose file is clicked',()=>{
  const submit=vi.fn((event:SubmitEvent)=>event.preventDefault())
  render(<form onSubmit={submit}><YouTubeVideoPicker disabledReason={null} onRegister={vi.fn(async()=>{})}/></form>)
  const input=screen.getByLabelText('Local video file') as HTMLInputElement
  vi.spyOn(input,'click').mockImplementation(()=>{})
  fireEvent.click(screen.getByRole('button',{name:'Choose file'}))
  expect(submit).not.toHaveBeenCalled()
 })

 it('keeps registration disabled with the exact prerequisite shown',()=>{
  const reason='Approve the current content revision before registering its YouTube video.'
  render(<YouTubeVideoPicker disabledReason={reason} onRegister={vi.fn(async()=>{})}/>)
  fireEvent.change(screen.getByLabelText('Local video file'),{target:{files:[videoFile()]}})
  expect((screen.getByRole('button',{name:'Upload / register video'}) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText(reason)).toBeTruthy()
 })
})