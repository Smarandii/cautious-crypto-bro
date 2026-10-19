# Local state backup and restore drill

Back up the application SQLite database and the Telethon SQLite session before
upgrading or migrating. `scripts/state_backup.py` uses SQLite's backup API, so
committed WAL data is included. It takes the same state-owner lock as the app and
refuses to proceed while a cooperating app owns that database. Stop all other
writers too; older apps and unrelated operator scripts may not take this lock.

The archive contains `database.sqlite3` and `telegram.session`, plus optional
`telegram-identity.json` when the reader account has been pinned. Creation
automatically restores them into disposable scratch space, validates the identity
record when present, checks SQLite
integrity/required tables and records SQLite `PRAGMA user_version` before success. It never
migrates state, calls external APIs, prints database/session contents or replaces
an existing archive. Failed creation removes only its own incomplete archive.
The receipt contains the archive SHA-256, schema/table counts and verification
result. Keep the receipt with the archive on protected storage: the session file
contains authentication material. POSIX archives are created with mode `0600`;
Windows copies inherit the destination's filesystem access controls.

Redis evaluation caches/provider cooldowns are not included and can repopulate.
Preserve `.env` separately in protected storage. This is a local-state backup,
not a copy of Bybit's positions/orders or proof of authenticated readiness.

## Existing local Docker Compose deployment

Use a clean checkout and the same Compose project as the deployed app. These
PowerShell commands assume the default `/state` volume and filenames; adapt the
two source paths if configured differently. The helper image is separate from
the deployed image. No new app instance is started.

```powershell
$backupHelper = "cautious-crypto-bro:backup-helper-$(git rev-parse --short HEAD)"
docker build --target runtime --tag $backupHelper .
if ($LASTEXITCODE -ne 0) { throw "Helper build failed" }

New-Item -ItemType Directory -Path backups -Force | Out-Null
$backupRoot = (Resolve-Path backups).Path
$backupName = "state-$(Get-Date -Format 'yyyyMMdd-HHmmssfff').tgz"
$appContainer = docker compose ps -aq app
if ($LASTEXITCODE -ne 0 -or -not $appContainer) { throw "Existing app container required" }
$stateVolume = docker inspect --format '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Name}}{{end}}{{end}}' $appContainer
if ($LASTEXITCODE -ne 0 -or -not $stateVolume) { throw "Named /state volume required" }
docker inspect --format '{{.Image}}' $appContainer | Out-File -NoClobber -ErrorAction Stop "backups/$backupName.image.txt"
if ($LASTEXITCODE -ne 0) { throw "Cannot record deployed image" }

docker compose stop app
if ($LASTEXITCODE -ne 0) { throw "Cannot stop app" }
$backupReceiptText = docker run --rm --network none `
  --mount "type=volume,source=$stateVolume,target=/state" `
  --mount "type=bind,source=$backupRoot,target=/backups" `
  --entrypoint /app/.venv/bin/python $backupHelper /app/scripts/state_backup.py create `
  --database /state/cautious_crypto_bro.sqlite3 `
  --session /state/cautious_crypto_bro.session --output "/backups/$backupName"
if ($LASTEXITCODE -ne 0) { throw "Backup failed; app remains stopped" }
$backupReceiptText | Set-Content -Encoding utf8 "backups/$backupName.receipt.json"
$backupReceipt = $backupReceiptText | ConvertFrom-Json
$copiedHash = (Get-FileHash "backups/$backupName" -Algorithm SHA256).Hash.ToLowerInvariant()
if ($copiedHash -ne $backupReceipt.archive_sha256) { throw "Host archive hash mismatch" }
```

The source volume is writable so the stable owner-lock file can be acquired;
database connections use read-only mode. Run a second restore drill using only the archive
mount, with no original state volume or network access:

```powershell
docker run --rm --network none `
  --mount "type=bind,source=$backupRoot,target=/backups,readonly" `
  --entrypoint /app/.venv/bin/python $backupHelper /app/scripts/state_backup.py verify `
  "/backups/$backupName" --sha256 $backupReceipt.archive_sha256
if ($LASTEXITCODE -ne 0) { throw "Restore drill failed; app remains stopped" }
```

If continuing with the same version, `docker compose start app` starts the
existing stopped container; it does not deploy the helper image. Check startup,
source connectivity, recovery warnings and protection before calling the app
ready. Keep a copy of the archive and receipt outside Docker's data disk so losing
that disk does not lose the backup too.

## Restore or rollback

`verify` always restores into temporary space and removes that space afterward;
it cannot overwrite live state. For an actual restore, stop all writers, preserve
the current state first, and prepare a new empty state directory/volume. After
checking the saved SHA-256, extract the verified regular files there with a trusted
tar tool. Map `database.sqlite3` to the configured `DATABASE_PATH` basename and
`telegram.session` to the configured Telethon `.session` filename. Map optional
`telegram-identity.json` to `DATABASE_PATH` with suffix `.telegram-identity.json`;
preserve it to retain the expected reader account across restores. Legacy
two-file backups remain verifiable. Do not copy
old journals, WAL files or an owner-lock file over an existing database.

Retain the matching image or a source revision that can rebuild it. Record and
select the matching app image/schema. Never downgrade only
`PRAGMA user_version`. An older local snapshot does not rewind the exchange:
reconcile all positions, orders and protection changed since backup before
resuming. Require fresh Bybit Demo/Telegram connectivity and startup
reconciliation checks before cutover. See [STRATEGY.md](../STRATEGY.md) for schema
compatibility and rollback constraints.
