# Local app and publisher cutover

The publisher is an opt-in sibling service. `restart: unless-stopped` does not
restart a deliberately stopped container. Do not split an app deployment and
publisher resumption across separate operator or agent invocations.

For an ordinary image update with an explicitly verified unchanged state schema,
use one foreground operation with this sequence:

1. Build and verify the new runtime image before stopping anything. Record both
   existing image IDs and revision labels; retain them for rollback. Check that
   the existing app and publisher are healthy and no execution claim or publisher
   export is in flight. Keep the same project, environment and named volumes.
2. Prepare two private Compose override files, selecting the new image for **both**
   services and the recorded previous image for each service respectively. Include
   their matching revision labels. Do not print rendered Compose environments or
   put secrets in these files.
3. In the same foreground operation, stop the publisher first, then the app.
   Create and restore-verify the state backup described in
   [state-backups.md](state-backups.md), including its host SHA-256. Preserve the
   SQLite/WAL maintenance boundary; do not run the read-only publisher during it.
4. Start both services with one Compose invocation. Redis is already running;
   `--no-deps` prevents unrelated service changes. Verify app authentication,
   reader startup, supervision and native stops; verify the publisher's exact
   revision, health and a new successful public receipt before declaring delivery.
5. If backup, start or readiness fails, the **same operation** must attempt to
   resume both services using the recorded compatible images. Report the original
   failure even when rollback succeeds. Verify the recovered services; never
   describe a successful `up` command alone as authenticated recovery.

For example, the start and rollback commands inside that foreground operation
are below. `$newOverride` and `$rollbackOverride` refer to the prepared files;
they must select both services. The catch must also cover the stop, backup and
readiness steps above, not just these commands.

```powershell
$composeBase = @("compose", "-p", "cautious-crypto-bro", "-f", "docker-compose.yaml")
$newCompose = $composeBase + @("-f", $newOverride, "--profile", "publisher")
$oldCompose = $composeBase + @("-f", $rollbackOverride, "--profile", "publisher")

# Within the existing foreground backup/cutover try/catch:
docker @newCompose up -d --no-deps --no-build app publisher
if ($LASTEXITCODE -ne 0) { throw "Cutover failed; recover both services" }

# In that operation's catch, for a verified schema-compatible rollback:
docker @oldCompose up -d --no-deps --no-build app publisher
if ($LASTEXITCODE -ne 0) { throw "Recovery failed; inspect both services immediately" }
```

Do not execute the rollback example after a successful cutover. These commands
illustrate the two branches of the operation, not a sequential command recipe.
The publisher profile remains opt-in: deployments without an existing configured
publisher must not enable publication as a side effect of an app update.

## Failure and maintenance boundaries

Compose startup is not a transaction. A forced process kill, host failure or
Docker shutdown can interrupt the operation before either branch finishes. On
resumption, inspect `docker compose --profile publisher ps -a`, the image IDs,
state schema and saved backup receipt. Explicitly resume both verified compatible
services with the appropriate override; then perform the readiness checks above.
Do not blindly rerun a backup or start a second app against the same state.

A schema migration or state restore needs its own verified compatibility and
rollback procedure. Never automatically start an old image against newly migrated
state. If compatibility cannot be established, leave an explicit operator recovery
failure rather than trying an arbitrary image. This runbook does not authorize
changing risk, entry/exit rules, approvals or historical signals.

The publisher keeps its read-only state/secrets mounts, private work volume,
singleton lock, publication verification and Docker ownership. No host scheduler,
visible background console or Docker socket is added. A new receipt can take a
full export/publication cycle; observe that running cycle rather than restarting
it merely because health is still `starting`.
