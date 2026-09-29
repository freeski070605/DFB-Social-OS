# Mobile access: Vercel frontend and local FastAPI

## Architecture and existing behavior

The recommended public browser origin is `https://social.dfbsolutions.co`. Vercel serves the Vite build and rewrites `/api/*` and `/health` to the permanent Cloudflare Tunnel origin, `https://social-api.dfbsolutions.co`. Cloudflare Tunnel forwards only those paths to FastAPI at `http://127.0.0.1:8000`. SQLite, scheduler, workers, Ollama, media, DFB AI Studio, encryption keys, and provider credentials stay on the Windows PC.

The frontend currently uses relative `/api` URLs and includes credentials. `VITE_API_BASE_URL` is optional and defaults to empty; **leave it empty in the recommended deployment**. Local Vite proxies `/api` and `/health` to FastAPI. FastAPI also serves a built frontend locally, including its SPA fallback. Vercel serves the frontend independently and provides its own SPA fallback.

Sessions use a host-only `dfb_session` cookie (no `Domain`), `HttpOnly`, `SameSite=Lax`, path `/`, and 12-hour maximum age. `Secure` follows `DFB_COOKIE_SECURE`; set it to `true` before public HTTPS use. Authenticated mutations require the session CSRF token. Both provider OAuth flows bind a one-use state to the admin and session. CORS currently allows only configured origins, credentials, GET/POST/PUT/DELETE, and the needed headers. Production should add only `https://social.dfbsolutions.co` to the existing allowed origins. Do not use `*`.

The OAuth redirect URI is taken verbatim from the local `.env`; changing this repository does not change it. The permanent callbacks below use the **browser origin** so the same host-only session cookie reaches the callback. A callback to `social-api.dfbsolutions.co` would not carry a cookie created on `social.dfbsolutions.co`.

The tunnel API hostname is publicly routable. FastAPI authentication, CSRF checks, and origin checks still apply there. Cloudflare ingress rules limit it to `/api/*` and `/health`. The Vercel rewrite must be checked on the deployed domain before switching OAuth callback registrations: verify that `Set-Cookie` stays host-only on `social.dfbsolutions.co`, login works, CSRF protected mutations work, and callback requests reach FastAPI with that session cookie. Vercel's external rewrite preserves the browser URL and does not cache external-origin responses unless cache headers allow it; the backend sends `no-store` for API and health responses. See [Vercel rewrites](https://vercel.com/docs/routing/rewrites).

## Vercel deployment

1. Put this repository in your Git provider. In Vercel, import it as a **Vite** project and set **Root Directory** to `frontend`.
2. Use `npm run build` as the build command and `dist` as the output directory. Vercel installs from `frontend/package-lock.json`.
3. Do not add backend secrets to Vercel. No Vercel environment variables are required. Leave `VITE_API_BASE_URL` unset or empty. Only public `VITE_*` values are bundled, and no provider credentials belong there.
4. Attach `social.dfbsolutions.co` to the Vercel project and follow Vercel's displayed DNS instructions for that hostname. The Cloudflare Tunnel hostname below uses a separate DNS record.
5. Turn on HTTPS and ensure the production domain resolves. Keep preview deployments protected or use only the production hostname for admin work: the production CORS list and OAuth redirect URIs are intentionally exact.
6. After the tunnel is running, check `https://social.dfbsolutions.co/health` returns `{"status":"ok"}`. Then test login, sign out, and a harmless authenticated read. An unavailable PC or tunnel shows **Local backend offline**; the frontend shell may still load.

The checked-in `frontend/vercel.json` is fixed to `social-api.dfbsolutions.co`. If a different permanent tunnel hostname is chosen, edit its two rewrite destinations before deploying. Never point it to a temporary `trycloudflare.com` URL.

## Named Cloudflare Tunnel on Windows

Install `cloudflared` on the Windows PC following [Cloudflare's installation guide](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/). In PowerShell as the Windows account that runs the backend:

```powershell
cloudflared tunnel login
cloudflared tunnel create dfb-social-os
cloudflared tunnel list
cloudflared tunnel route dns dfb-social-os social-api.dfbsolutions.co
```

These commands create Cloudflare credentials and the `social-api` DNS route. Run them manually only when ready; no Cloudflare changes are made by this repository. Place this `config.yml` at `$env:USERPROFILE\.cloudflared\config.yml`, replacing `<TUNNEL-UUID>` and `<WINDOWS-USER>` with the actual values:

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: C:\Users\<WINDOWS-USER>\.cloudflared\<TUNNEL-UUID>.json
ingress:
  - hostname: social-api.dfbsolutions.co
    path: '^/api(?:/.*)?$'
    service: http://127.0.0.1:8000
  - hostname: social-api.dfbsolutions.co
    path: '^/health$'
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Start FastAPI with `.\scripts\run.ps1` and keep it running. Then validate and run the tunnel:

```powershell
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://social-api.dfbsolutions.co/health
cloudflared tunnel run dfb-social-os
```

If you want the tunnel to start with Windows, follow [Cloudflare's Windows service instructions](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/windows/). The service account must be able to read the tunnel credentials and config. Installing only the tunnel service does **not** start FastAPI; arrange for the backend to start under the correct Windows account as well. Keep its bind address at `127.0.0.1`, never `0.0.0.0` merely for the tunnel. Keep Ollama and DFB AI Studio bound locally.

## Local `.env` changes at cutover

Edit the existing `.env` in place. Retain `DFB_ENCRYPTION_KEY`, the existing provider secrets, SQLite URL, brand selections, and scheduler settings. Set:

```dotenv
DFB_COOKIE_SECURE=true
DFB_ALLOWED_ORIGINS=["http://127.0.0.1:8000","http://localhost:8000","http://localhost:5173","http://127.0.0.1:5173","https://social.dfbsolutions.co"]
DFB_META_REDIRECT_URI=https://social.dfbsolutions.co/api/meta/callback
DFB_YOUTUBE_REDIRECT_URI=https://social.dfbsolutions.co/api/youtube/callback
```

Register the exact Meta URI above in **Meta for Developers → Facebook Login for Business → Valid OAuth Redirect URIs**. Register the exact Google URI above in **Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Web application → Authorized redirect URIs**. Add both provider-side URIs before changing the corresponding local `.env` values. Restart FastAPI after editing `.env`. Existing encrypted Facebook and YouTube credentials and selected accounts remain intact; this cutover changes callback settings for future connections only. Keep the existing callback URI registered during the transition if you still need it.

`Secure` cookies require HTTPS. If using the old local `http://127.0.0.1:8000` browser URL after cutover, you may need a separate development configuration with `DFB_COOKIE_SECURE=false` for login. The recommended admin URL after cutover is the HTTPS production origin.

## Install on a phone

Open `https://social.dfbsolutions.co` on the phone and sign in. In Chrome on Android, use **Install app** or **Add to Home screen** from the browser menu. In Safari on iPhone, use **Share → Add to Home Screen**. The app's cached shell can open without the PC, but it shows **Local backend offline** and disables backend actions. It does not queue mutations or publishing. The PC, FastAPI process, and tunnel must all be running for normal use.
