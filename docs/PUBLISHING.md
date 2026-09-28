# Meta Connection and Publishing

Authoring, review, rendering, and manual export work without Meta credentials. Keep global and brand autopilot paused while configuring accounts.

## Meta app setup

1. In Meta for Developers, configure Facebook Login for Business and the products and permissions your account needs. The app must be allowed to access the Page, and the Instagram professional account must be linked to that Page. App review and account eligibility are controlled by Meta.
2. Register an exact HTTPS OAuth redirect URI, for example `https://YOUR_ADMIN_ORIGIN/api/meta/callback`. The admin must open DFB Social OS at that same HTTPS origin so its secure session cookie reaches the callback. An external HTTPS callback is required for a production Meta connection; the local image server does not need public exposure.
3. In the local `.env`, set `DFB_META_APP_ID`, `DFB_META_APP_SECRET`, `DFB_META_REDIRECT_URI`, and `DFB_COOKIE_SECURE=true`. Add your HTTPS admin origin to `DFB_ALLOWED_ORIGINS`. Retain `DFB_ENCRYPTION_KEY`. Restart the server after editing `.env`.
4. Sign in, select the intended brand, then open **Settings → Accounts → Connect Meta**. Consent in Meta's browser dialog. The one-use state expires after ten minutes and is bound to the signed-in admin session. The callback exchanges the code, requests available managed Pages, and discovers linked Instagram professional accounts. Page access tokens are encrypted in SQLite. A second brand cannot claim the same external account.
5. The first discovered Page and Instagram account are selected for this brand. If more appear, use **Use for publishing** on the intended account. Use **Check connection** to inspect identity, granted permissions, token validity, expiry metadata, and last check time. Meta may report no fixed Page token expiry; revocation is still possible.
6. Use **Reconnect** to repeat consent and replace credentials. **Disconnect** removes only the locally stored account and token; it does not revoke the Meta app's authorization. Revoke the app separately in Meta if you want platform-wide revocation.

The requested permissions cover Page listing and posting, Instagram identity and publishing, and supported comment workflows. Messaging or other permissions may require additional app configuration and review. A healthy token does not guarantee every endpoint is authorized. Manual account ID and token entry remains under **Advanced**; it is encrypted and never returned to the browser after entry. Manual tokens are not automatically renewed.

## Public media

Instagram image/carousel and Facebook photo publishing require Meta to fetch images from a publicly retrievable HTTPS URL. DFB Social OS no longer serves publishing images from its local server. The `PublicMediaProvider` interface accepts only rendered images attached to approved content. The shipped `LocalUnavailablePublicMediaProvider` rejects image publishing before a Meta container or post is created.

The `.env` accepts reserved S3-compatible settings: `DFB_S3_ENDPOINT`, `DFB_S3_BUCKET`, `DFB_S3_REGION`, `DFB_S3_ACCESS_KEY`, `DFB_S3_SECRET_KEY`, and `DFB_S3_PUBLIC_BASE_URL`. Keep credentials in `.env`, which is excluded from Git and backups. These settings do **not** enable S3 uploads in this release. Install a vetted remote provider adapter before setting `DFB_PUBLIC_MEDIA_PROVIDER` to anything other than `local_unavailable`. That adapter must upload only approved publishing assets, return HTTPS URLs Meta can fetch, and enforce expiry/cleanup according to the bucket policy. No local route exposes arbitrary files.

Facebook text publishing and manual export do not require public media. Instagram image publishing and Facebook photo publishing are unavailable until a remote provider is implemented. Do not point Meta at a private localhost URL.

## Webhooks

Set `DFB_META_VERIFY_TOKEN` and `DFB_META_APP_SECRET`, then register `https://YOUR_PUBLIC_ORIGIN/api/webhooks/meta` in the Meta app. Webhooks need an independently reachable HTTPS callback. Incoming bodies are size limited and checked using Meta's `X-Hub-Signature-256`. Without webhooks, community interactions can be entered manually.

## Publishing safety

Content must be approved. A brand and global pause both block outward calls. Set publishing permission to `APPROVAL` for early use, select the intended Page, inspect permissions, and use a test item only when a real post is intended. An uncertain request becomes `UNKNOWN`; inspect Meta directly and reconcile with the real external ID or verified absence before retrying. Never use live posting as an automated test. Story, Reel, and video script formats are manual export only.

Meta reference: [Meta's official Instagram API collection for Page token and linked account discovery](https://www.postman.com/meta/instagram/request/lpx8lul/get-access-tokens-of-pages-you-manage).
