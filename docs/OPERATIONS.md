# Operations Guide

## Ollama and Manual Mode

For local generation, install the current Ollama for Windows release, start the Ollama service, then run:

```powershell
ollama pull qwen2.5:7b
ollama list
```

The default brand model is `qwen2.5:7b`; **Settings → Local AI** can select another installed model. `DFB_OLLAMA_URL` defaults to `http://127.0.0.1:11434`. Open **System** to check provider health. A health result of `offline` or `model_missing` means generation is unavailable; it does not prevent manual content editing, rendering, manual export, approvals, or local scheduling. AI generation also requires at least one enabled, approved knowledge entry relevant to the topic.

To operate without Ollama, leave or set the brand provider to **Manual authoring** and author structured content in the editor. The manual provider intentionally does not invent generated output. Do not use `/generate` as a manual authoring shortcut.

## Administrator and Brands

Create the first administrator after setup and migration, before starting the server:

```powershell
& .\.venv\Scripts\python.exe -m app.cli create-admin --username admin
```

The command prompts for a password and confirmation. Passwords require at least 12 characters. Admin creation is a local CLI operation; there is no unauthenticated web bootstrap endpoint. Login sessions use an HTTP-only cookie and CSRF token. For a local HTTP-only installation, `DFB_COOKIE_SECURE=false` is appropriate; set it true behind correctly configured HTTPS.

The `life_help` seed is loaded from `brands/life_help/brand.json` if that slug is absent from the database. Seeded brands start enabled by default, while each brand pause and the global autopilot pause start on. Select the brand under **Brands**, review and save its identity, then explicitly enable/unpause it when prepared. The UI's global pause blocks outward actions across brands; the brand pause blocks only that brand. Either pause is sufficient to stop outward actions.

## Safe Publishing Sequence

1. Verify the brand is enabled and its topics, voice, knowledge, schedule, targets, and permissions are correct.
2. Keep global and brand pauses on while reviewing drafts. Start with publishing permission `APPROVAL`; content itself always needs editorial approval.
3. Check local rendered previews for overflow and factual accuracy. The renderer rejects text overflow rather than silently clipping it.
4. Approve and schedule. Jobs persist in SQLite; a restart recovers ordinary interrupted jobs, while interrupted publish/reply jobs and in-flight publications become `UNKNOWN` for inspection.
5. On **Publishing**, inspect Meta directly before reconciling an unknown attempt. Record the real external post ID if it exists. Confirm absence only after verifying the platform did not create a post. Only then can a retry be safe.
6. Confirm the destination account and media accessibility before changing `publish` from `APPROVAL` to `AUTO`. Resume the brand and global autopilot separately. Keep the approval queue monitored.

The scheduler runs in one server process and the application lock prevents a second instance. `DFB_SCHEDULER_ENABLED=false` disables dispatch; it does not erase queued jobs. Scheduling uses the brand IANA timezone and checks publish windows and recent-topic repetition.

## Backups and Restore

The admin **Backups** page creates, downloads, lists, and restores backups. Restore makes a safety copy first and clears admin sessions; sign in again afterward. Restore forces global and brand autopilot pauses and invalidates pending Meta OAuth connections. SQLite backups include the database, brand JSON files, renderer font, and generated media. The database contains encrypted Meta account tokens and password hashes. The `.env` file, encryption key, Meta app secret, and any object-storage secrets are excluded. Preserve `DFB_ENCRYPTION_KEY` separately; without the original key, restored Meta tokens cannot be decrypted and accounts must be reconnected.

The offline PowerShell restore is preferable for recovery from a damaged or unavailable server:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\restore.ps1 -Archive .\data\backups\dfb-YYYYMMDD-HHMMSS.zip
```

Stop the server first. PostgreSQL is not supported by the built-in archive/restore service; use PostgreSQL-native tools instead. Test recovery with a non-production copy before relying on a backup.

## Known Boundaries

- Meta connection uses Facebook Login for managed Pages and their linked Instagram professional accounts. Manual tokens remain an advanced fallback. Reconnect through Meta when a token is revoked or expires; this Page-token integration does not claim a universal automatic refresh endpoint.
- Image publishing is disabled until a remote public-media provider is implemented and configured. Text publishing and manual export remain available.
- Supported direct publishing is Facebook text/photo posts and Instagram feed images/carousels. Stories, Reels, and video scripts are exported for manual publishing.
- Meta webhooks require a reachable HTTPS callback. Community actions can be entered manually when webhooks are not configured.
- SQLite is the supported local database. Only one application server process is permitted.
