# DFB Social OS

Private, local-first content operations for multiple brands. The FastAPI service serves the React admin app and a persistent SQLite-backed scheduler. Social network publishing is optional; manual authoring, graphics, and export work without Ollama or Meta credentials.

## Requirements

- Windows 10/11, PowerShell 5.1 or newer
- Python 3.12 (enable the Python launcher)
- Node.js LTS with npm
- Optional: Ollama for local AI generation
- Optional: Meta developer app and HTTPS OAuth callback; a separate remote public-media provider is required for image publishing

## Install and Start

Open PowerShell in the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
& .\.venv\Scripts\python.exe -m app.cli create-admin --username admin
.\scripts\run.ps1
```

The admin command prompts for a password; use at least 12 characters. Keep the run window open and browse to <http://127.0.0.1:8000>. Setup creates `.env` only when absent, generates an encryption key only when it is blank, installs dependencies, builds the frontend, migrates SQLite, and seeds the checked-in brand configurations. Do not replace `.env` or lose `DFB_ENCRYPTION_KEY`; account tokens cannot be decrypted without it.

Run future migrations with `Set-ExecutionPolicy -Scope Process Bypass; .\scripts\migrate.ps1`. Create a CLI backup with `Set-ExecutionPolicy -Scope Process Bypass; .\scripts\backup.ps1`. Stop the server before running `.\scripts\restore.ps1 -Archive .\data\backups\dfb-....zip`; restore creates a safety backup, invalidates sessions, and pauses all brands. Backups include media and encrypted tokens, but exclude .env and the encryption key.

## First-Run Workflow

1. Sign in with the administrator credentials you just created.
2. Select **Brands** and choose **LIFE, APPARENTLY.** Its existing `life_help` slug is an internal identifier. Review its identity and keep its pause enabled.
3. Add sourced entries under **Knowledge**. Mark only checked, reliable entries **APPROVED** and enabled. Approved knowledge is required for AI generation; manual writing does not require Ollama.
4. Create content from **Content** or **Create**. Write manually or request local generation. Inspect every claim and source, then render graphics when needed. Instagram publishing requires rendered images.
5. Send a draft to review, then approve it in the editor after checking the exact content. Schedule approved content on **Calendar**, or export a ZIP from the editor. The ZIP contains structured content, caption, and rendered slide images.
6. Configure Meta under **Settings → Accounts → Connect Meta** only when ready. Image publishing remains unavailable until a remote public-media adapter is installed. **Publishing** shows attempt states and requires a verified reconciliation before retrying ambiguous outcomes.
7. **System** shows provider health and global/per-brand pause controls. Keep publishing in **APPROVAL** mode for initial operation. Unpause globally and for a brand only after confirming its rules, accounts, and approval queue; AUTO publication is an explicit risk decision.

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for Ollama, first admin, manual-mode, scheduling, and recovery details, and [docs/PUBLISHING.md](docs/PUBLISHING.md) for Meta scope, token, webhook, and media setup. Feature coverage and adapter limits are documented there; not every Meta content type is publishable through this integration.

For optional HTTPS desktop/mobile access with a Vercel frontend and named Cloudflare Tunnel, see [docs/MOBILE_ACCESS.md](docs/MOBILE_ACCESS.md). Deployment and DNS changes are manual.
