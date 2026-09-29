export function readableError(reason: unknown): string {
  const message = reason instanceof Error ? reason.message : String(reason)
  try {
    const details = JSON.parse(message) as {loc: (string | number)[]; type: string}[]
    if (!Array.isArray(details)) return message
    return details.map(detail => {
      const parts = detail.loc[0] === 'body' ? detail.loc.slice(1) : detail.loc
      const field = parts[0] === 'slides'
        ? `Slide ${Number(parts[1]) + 1} ${String(parts[2] || 'field')}`
        : parts.join(' ').replaceAll('_', ' ')
      const problem = detail.type === 'missing' ? 'is required'
        : detail.type === 'string_too_short'
          ? parts[0] === 'slides' && parts[2] === 'title' ? 'is required' : 'is too short'
        : detail.type === 'literal_error' ? 'has an unsupported value'
        : detail.type === 'string_too_long' ? 'is too long' : 'is invalid'
      return `${field} ${problem}`
    }).join('; ')
  } catch {
    return message
  }
}
