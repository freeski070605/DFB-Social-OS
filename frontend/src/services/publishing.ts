export const publishingPlatforms = ['facebook', 'instagram'] as const

export type PublishingPlatform = typeof publishingPlatforms[number]
export type FacebookPostType = 'TEXT' | 'SINGLE_IMAGE' | 'MULTI_IMAGE'
export type InstagramPostType = 'SINGLE_IMAGE' | 'CAROUSEL'
export type PublishingPostType = FacebookPostType | InstagramPostType

export interface PublishingMedia {
  key: string
  preview?: string
}

export interface GraphStep {
  method: string
  path: string
  purpose: string
}

interface ResultBase {
  platform: PublishingPlatform
}

export interface ReadyPublishingResult extends ResultBase {
  status: 'READY'
  destination: string
  post_type: PublishingPostType
  media_count: number
  media: PublishingMedia[]
  text: string
  expires_at: string
  blockers: string[]
  graph_steps: GraphStep[]
  plan_token: string
  action: string
}

export interface BlockedPublishingResult extends ResultBase {
  status: 'BLOCKED'
  blockers: string[]
}

export interface ErrorPublishingResult extends ResultBase {
  status: 'ERROR'
  message: string
  blockers: string[]
}

export type PublishingResult = ReadyPublishingResult | BlockedPublishingResult | ErrorPublishingResult

const displayName: Record<PublishingPlatform, string> = {
  facebook: 'Facebook',
  instagram: 'Instagram',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

function contractError(platform: PublishingPlatform): ErrorPublishingResult {
  return {platform, status: 'ERROR', message: `${displayName[platform]} dry-run result could not be displayed.`, blockers: []}
}

export function requestError(platform: PublishingPlatform, message: string): ErrorPublishingResult {
  return {platform, status: 'ERROR', message: `${displayName[platform]} dry-run request failed: ${message}`, blockers: []}
}

function blockersFrom(value: unknown): string[] | null {
  if (value === undefined || value === null) return []
  if (!Array.isArray(value) || !value.every((reason) => typeof reason === 'string')) return null
  return value
}

function graphStepsFrom(value: unknown): GraphStep[] | null {
  if (value === undefined || value === null) return []
  if (!Array.isArray(value)) return null
  const steps: GraphStep[] = []
  for (const step of value) {
    if (!isRecord(step) || !isNonEmptyString(step.method) || !isNonEmptyString(step.path) ||
        !isNonEmptyString(step.purpose)) return null
    steps.push({method: step.method, path: step.path, purpose: step.purpose})
  }
  return steps
}

function mediaFrom(value: unknown): PublishingMedia[] | null {
  if (!Array.isArray(value)) return null
  const media: PublishingMedia[] = []
  for (const asset of value) {
    if (!isRecord(asset) || !isNonEmptyString(asset.key) ||
        (asset.preview !== undefined && typeof asset.preview !== 'string')) return null
    media.push({key: asset.key, ...(asset.preview === undefined ? {} : {preview: asset.preview})})
  }
  return media
}

function postType(platform: PublishingPlatform, action: string, mediaCount: number): PublishingPostType | null {
  if (platform === 'facebook') {
    if (action === 'Facebook Page text' && mediaCount === 0) return 'TEXT'
    if (action === 'Facebook Page single image' && mediaCount === 1) return 'SINGLE_IMAGE'
    if (action === 'Facebook Page multi image' && mediaCount > 1) return 'MULTI_IMAGE'
    return null
  }
  if (action !== 'Instagram image/carousel') return null
  if (mediaCount === 1) return 'SINGLE_IMAGE'
  return mediaCount > 1 ? 'CAROUSEL' : null
}

function readyPlan(
  platform: PublishingPlatform,
  plan: Record<string, unknown>,
  planToken: unknown,
  localMedia: unknown,
): ReadyPublishingResult | ErrorPublishingResult {
  const accountId = plan.account_id
  const destinationId = plan.destination_id
  const accountName = plan.account_name
  const action = plan.action
  const caption = plan.caption
  const mediaCount = plan.media_count
  const contentId = plan.content_id
  const revision = plan.revision
  if (plan.platform !== platform || typeof accountId !== 'number' || !Number.isInteger(accountId) || accountId < 1 ||
      !isNonEmptyString(destinationId) ||
      (accountName !== undefined && accountName !== null && typeof accountName !== 'string') ||
      !isNonEmptyString(action) || typeof caption !== 'string' || !isNonNegativeInteger(mediaCount) ||
      typeof contentId !== 'number' || !Number.isInteger(contentId) || contentId < 1 ||
      typeof revision !== 'number' || !Number.isInteger(revision) || revision < 1 ||
      typeof planToken !== 'string' || !/^[0-9a-f]{32}$/i.test(planToken)) return contractError(platform)

  const steps = graphStepsFrom(plan.graph_steps)
  const blockers = blockersFrom(plan.blockers)
  const media = mediaFrom(localMedia)
  if (!steps || !blockers || !media || media.length !== mediaCount) return contractError(platform)

  const normalizedPostType = postType(platform, action, mediaCount)
  if (!normalizedPostType) return contractError(platform)
  const expiresAt = plan.expires_at
  if (typeof expiresAt !== 'string' || !Number.isFinite(Date.parse(expiresAt))) return contractError(platform)

  return {
    platform,
    status: 'READY',
    destination: isNonEmptyString(accountName) ? accountName : destinationId,
    post_type: normalizedPostType,
    media_count: mediaCount,
    media,
    text: caption,
    expires_at: expiresAt,
    blockers,
    graph_steps: steps,
    plan_token: planToken,
    action,
  }
}

export function normalizeDryRunResponses(
  value: unknown,
  platforms: readonly PublishingPlatform[],
  localMedia: readonly PublishingMedia[],
): PublishingResult[] {
  if (!isRecord(value) || !['READY', 'BLOCKED', 'ERROR'].includes(String(value.status))) {
    return platforms.map(contractError)
  }

  const responseBlockers = blockersFrom(value.reasons)
  if (!responseBlockers) return platforms.map(contractError)

  const plansValue = value.plan ?? []
  if (!Array.isArray(plansValue)) return platforms.map(contractError)
  if (value.status === 'ERROR') return platforms.map(contractError)
  if (value.status === 'BLOCKED') {
    return platforms.map((platform) => ({platform, status: 'BLOCKED', blockers: responseBlockers}))
  }

  return platforms.map((platform) => {
    const matchingPlans = plansValue.filter((plan): plan is Record<string, unknown> =>
      isRecord(plan) && plan.platform === platform)
    if (matchingPlans.length !== 1) return contractError(platform)
    return readyPlan(platform, matchingPlans[0], value.plan_token, localMedia)
  })
}

export function normalizeDryRunResponse(
  value: unknown,
  platform: PublishingPlatform,
  localMedia: readonly PublishingMedia[],
): PublishingResult {
  return normalizeDryRunResponses(value, [platform], localMedia)[0] ?? contractError(platform)
}