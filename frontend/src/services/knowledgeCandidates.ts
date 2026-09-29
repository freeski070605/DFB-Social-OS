import type {Knowledge} from '../types'

export function eligibleCandidates(rows: Knowledge[], brandId: number, pillar: string): Knowledge[] {
  const normalized = (value: string) => value.normalize('NFKC').replace(/\s+/g, ' ').trim().toLocaleLowerCase()
  return rows.filter(row => row.brand_id === brandId && normalized(row.category) === normalized(pillar) &&
    row.verification === 'APPROVED' && row.enabled)
}
