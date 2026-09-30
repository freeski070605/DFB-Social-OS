export function visibleYoutubeState(
  upload: {state: string; provider_video_id: string | null} | null,
  receipt: {state: string; external_id: string} | undefined,
): string | null {
  if (!upload) return null
  if (upload.state === 'PUBLISHED' &&
      (receipt?.state !== 'PUBLISHED' || receipt.external_id !== upload.provider_video_id)) return 'PROCESSING'
  return upload.state
}
