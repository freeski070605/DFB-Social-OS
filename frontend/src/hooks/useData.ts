import {useCallback, useEffect, useState} from 'react'
import {api} from '../services/api'
export function useData<T>(path: string, version = 0) {
  const [data, setData] = useState<T | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(true)
  const reload = useCallback(async () => {setLoading(true); try {setData(await api<T>(path)); setError('')} catch(e) {setError((e as Error).message)} finally {setLoading(false)}}, [path])
  useEffect(() => {void reload()}, [reload, version])
  return {data, error, loading, reload}
}
