# Meta Connection and Publishing

Authoring, review, rendering, and manual export work without Meta credentials. Keep global and brand autopilot paused while configuring accounts.

## Meta app setup

1. In Meta for Developers, configure Facebook Login for Business with a **System-user access token** configuration. Select the Facebook Pages and Instagram accounts assets and the permissions needed for the actions you intend to use. Copy the Configuration ID. The app must be allowed to access the Page, and the Instagram professional account must be linked to that Page. App review and account eligibility are controlled by Meta.
2. Register an exact HTTPS OAuth redirect URI, for example `https://YOUR_ADMIN_ORIGIN/api/meta/callback`. The admin must open DFB Social OS at that same HTTPS origin so its secure session cookie reaches the callback. An external HTTPS callback is required for a production Meta connection; the local image server does not need public exposure.
3. In the local `.env`, set `DFB_META_APP_ID`, `DFB_META_APP_SECRET`, `DFB_META_CONFIG_ID`, `DFB_META_REDIRECT_URI`, and `DFB_COOKIE_SECURE=true`. Add your HTTPS admin origin to `DFB_ALLOWED_ORIGINS`. Retain `DFB_ENCRYPTION_KEY`. Restart the server after editing `.env`.
4. Sign in, select the intended brand, then open **Settings → Accounts → Connect Meta**. Consent in Meta's browser dialog. The one-use state expires after ten minutes and is bound to the signed-in admin session. The callback exchanges the code for the Business Login credential, requests available Pages, and discovers linked Instagram professional accounts. Page tokens, or the system-user credential where Meta supplies no Page token, are encrypted in SQLite. A linked Instagram identity references its Page credential. A second brand cannot claim the same external account.
5. No discovered Page or Instagram account is selected automatically. Use **Select account** on the intended account for each platform. Use **Check connection** to inspect identity, granted permissions, token validity, expiry metadata, and last check time. Meta may report no fixed token expiry; revocation is still possible.
   On the selected Facebook Page, use **Check linked Instagram** to inspect `/{page_id}?fields=id,name,instagram_business_account` with its stored encrypted credential. The owner-only result reports HTTP status, safe Meta error codes, granted scopes, and whether Meta exposes an Instagram ID. A successful Page response with no Instagram field is ambiguous when `instagram_basic` is absent: inspect the Facebook Login for Business configuration and Instagram asset access before changing OAuth or the Page link. A discovered Instagram account is registered but must be selected explicitly.
6. Use **Connect Meta** again to repeat consent and replace credentials. **Disconnect** removes only the locally stored account and token; it does not revoke the Meta app's authorization. Revoke the app separately in Meta if you want platform-wide revocation.

The Business Login configuration owns the requested permissions; Social OS does not add a `scope` parameter to its login URL. Configure Page listing and posting, Instagram identity and publishing, and supported comment workflows there as needed. Messaging or other permissions may require additional app configuration and review. A healthy token does not guarantee every endpoint is authorized. Manual account ID and token entry remains under **Advanced**; it is encrypted and never returned to the browser after entry. Manual tokens are not automatically renewed.

## Public media

Instagram image/carousel and Facebook photo publishing require Meta to fetch images from a publicly retrievable HTTPS URL. DFB Social OS no longer serves publishing images from its local server. The `PublicMediaProvider` interface accepts only rendered images attached to approved content. The shipped `LocalUnavailablePublicMediaProvider` rejects image publishing before a Meta container or post is created.

The R2 adapter uploads only approved rendered PNG/JPEG assets whose stored digest matches the local bytes. Instagram publishing converts the current PNG render to JPEG in memory before upload. Set these server-side `.env` values and restart the backend:

```dotenv
DFB_PUBLIC_MEDIA_PROVIDER=r2
DFB_R2_ACCOUNT_ID=<32-character Cloudflare account ID>
DFB_R2_ACCESS_KEY_ID=<R2 token access key ID>
DFB_R2_SECRET_ACCESS_KEY=<R2 token secret access key>
DFB_R2_BUCKET=<dedicated publishing bucket>
DFB_R2_PUBLIC_BASE_URL=https://media.example.com
```

Create a **dedicated bucket containing only temporary publishing media** in Cloudflare R2. Create an R2 API token scoped to that bucket with object read/write/delete. Attach a production custom domain to that bucket in R2's Public Access settings, with HTTPS enabled, and use its root URL for `DFB_R2_PUBLIC_BASE_URL`. Do not enable the rate-limited `r2.dev` development URL for production. Do not put the existing private media library in this bucket. The S3 API endpoint is used only for authenticated backend uploads; it is not the public URL. The older `DFB_S3_*` settings are reserved and unused. No credentials or public R2 URL are returned through configuration APIs or backup exports.

R2 objects use random names under `meta-publish/`. Meta receives an ordinary public HTTPS URL with no signature or query string. Presigned R2 URLs are available only on the S3 API hostname, and Meta's public URL requirement does not establish that signed query URLs remain reliable throughout asynchronous processing. The dedicated bucket and short object lifecycle keep exposure narrow. Anyone with a temporary URL can fetch that object until deletion. Review Cloudflare caching settings so deleted objects do not remain cached; the adapter sets `Cache-Control: no-store`.

The publishing checkpoint records object keys before calling Meta. Published objects and definite failures become eligible for deletion after 24 hours. Transient processing and ambiguous outcomes remain available until retry or administrator reconciliation reaches a terminal result. The background worker retries failed deletions, including dry-run cleanup, after restart. Keep the scheduler enabled for automatic cleanup; if it is disabled, run cleanup operationally before relying on short retention. Do not delete media immediately after Instagram container creation or Facebook photo creation.

Facebook text publishing and manual export do not require public media. Instagram image publishing and Facebook photo publishing require this R2 configuration. Do not point Meta at a private localhost URL.

## Owner dry run and controlled test publish

Open **Create → an approved rendered content item → Manual Meta publishing dry run**. Choose Facebook, Instagram, or both. Confirm the brand, selected account name and ID, exact caption, and local media previews. Click **Run dry run**. It validates current account health and granted scopes, checks the rendered asset bytes, uploads temporary R2 objects, probes each public HTTPS URL with HEAD, builds a safe request plan, and removes the objects. It never calls a Meta publishing endpoint. It reports **READY** or **BLOCKED** with reasons. It can run while global or brand outward actions are paused. A successful media dry run verifies the current R2 configuration for 24 hours; image capabilities and real image publishing remain blocked when this verification is absent or expired.

If the public URL returns a non-200 response, check the R2 custom domain, bucket public access, HTTPS certificate, and Cloudflare rules. If an upload fails, check the bucket-scoped R2 token and backend environment values. If cleanup fails, the worker retries it. An unhealthy selected account needs **Settings → Accounts → Check connection**. Facebook Page posting requires `pages_manage_posts`; the currently granted permissions listed in this document do not include it. Instagram's Facebook Login flow requires `pages_show_list`, `pages_read_engagement`, `instagram_basic`, and `instagram_content_publish`; all four are present in the current grant, subject to the selected account remaining healthy and Meta account eligibility. Reel publishing is not implemented.

Only after reviewing a **READY** plan may the owner choose **Final confirmation: publish selected platforms**. The one-use plan expires after 30 minutes. The API rechecks the content revision, caption, media keys, selected account IDs, scopes and outward pause gates. Global or brand pause blocks the real action and consumes the plan; run dry run again after any pause change. Each platform has its own durable publication receipt, idempotency key, external ID or failure state. Publishing to both is two sequential external actions; an ambiguous outcome must be reconciled before retry. Do not use a live post to test setup.

## First private YouTube upload

The first YouTube publishing path is owner-confirmed and **PRIVATE only**. Public and unlisted visibility, playlists, thumbnails, scheduling, and autonomous YouTube publishing are not implemented. The global autonomous-action pause may remain on; the brand itself must be enabled with outward actions active.

The backend host must have `ffprobe` on `PATH` and durable read/write access to `DFB_STORAGE_ROOT` (default `data/media`). Video files are copied to local storage in bounded chunks, hashed with SHA-256, and probed for duration and dimensions. Back up that storage together with the database. From the repository root, run `.\scripts\migrate.ps1` in PowerShell, then restart the backend. For a hosted deployment, provision `ffprobe` on the backend image and persist the configured media storage path.

1. Keep global autonomous actions paused. Ensure the LIFE, APPARENTLY. brand is enabled and outward actions are active.
2. In **Settings → Accounts**, verify the intended YouTube channel, confirm its status is **PUBLISHING READY**, then activate/select that account for the brand.
3. Open an **APPROVED** content item at its current revision in **Create**.
4. In **Manual YouTube publishing**, choose the finished local video. Wait for the filename, duration, resolution, and size to appear.
5. Enter a title (up to 100 characters), description (up to 5,000 characters), intended format, and an explicit made-for-kids audience selection. Privacy is locked to **PRIVATE**.
6. Click **Run YouTube dry run**. Review the READY channel, asset SHA-256, metadata, and expiry. This validates locally and makes no YouTube mutation request.
7. Click **Final confirmation: upload PRIVATE video** and accept the warning: **THIS VIDEO WILL BE UPLOADED TO YOUTUBE AS PRIVATE.**
8. Keep the page open to monitor PREPARING, UPLOADING, PROCESSING, and PUBLISHED. If an interrupted attempt remains UPLOADING, use **Resume saved upload** to continue the same session. If it becomes RECONCILIATION REQUIRED, do not retry or start another upload; inspect the channel and resolve the ambiguous attempt first.

The READY plan expires after 20 minutes. A confirmed upload creates a YouTube receipt when YouTube returns a video ID, but the UI remains PROCESSING until YouTube reports `processingStatus=succeeded`. The receipt contains no OAuth token. The test suite uses fake providers and does not change production videos or receipts.

## Webhooks

Set `DFB_META_VERIFY_TOKEN` and `DFB_META_APP_SECRET`, then register `https://YOUR_PUBLIC_ORIGIN/api/webhooks/meta` in the Meta app. Webhooks need an independently reachable HTTPS callback. Incoming bodies are size limited and checked using Meta's `X-Hub-Signature-256`. Without webhooks, community interactions can be entered manually.

## Publishing safety

Content must be approved. A brand and global pause both block outward calls. Set publishing permission to `APPROVAL` for early use, select the intended Page, inspect permissions, and use a test item only when a real post is intended. An uncertain request becomes `UNKNOWN`; inspect Meta directly and reconcile with the real external ID or verified absence before retrying. Never use live posting as an automated test. Story, Reel, and video script formats are manual export only.

Meta reference: [Meta's official Instagram API collection for Page token and linked account discovery](https://www.postman.com/meta/instagram/request/lpx8lul/get-access-tokens-of-pages-you-manage).
