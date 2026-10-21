# Runbook: operating a hosted SIA server

Short, step by step. Commands assume the Docker image (`SIA_DB=/data/sia.db`); on a plain install, run them from
the server's directory with the same `.env`. `sia` reads `SIA_DB`, `SIA_BASE_URL` and `SIA_ADMIN_KEY` from the
environment.

## Back up

1. Run a backup. It is safe while the server is running (SQLite's online backup API, WAL mode):
   ```bash
   sia backup --dir /data/backups --keep 14
   ```
   It writes `sia-YYYYMMDD-HHMMSS.db` (UTC), runs `PRAGMA integrity_check` on the copy, deletes all but the newest
   14, and exits non-zero on any failure. A copy that fails the check is deleted and the older backups are kept.
2. Schedule it daily, and alert when it fails. Example crontab (`crontab -e` as the user that runs SIA):
   ```cron
   # 02:15 UTC every day; a non-zero exit sends cron's mail, or wire the || branch to your alerting
   15 2 * * * cd /srv/sia && sia backup --dir /data/backups --keep 14 >> /var/log/sia-backup.log 2>&1 || echo "sia backup failed" | mail -s "SIA backup failed" you@example.com
   ```
   With Docker: `docker exec sia sia backup --dir /data/backups --keep 14`.
3. Copy the backup directory off the machine (another disk, S3, your laptop). A backup on the same disk as the
   database doesn't survive losing that disk. For continuous replication, see `examples/deploy/litestream.yml`.
4. Once a month, do a restore drill on another machine (below) and check the row counts look right.

## Daily maintenance

`sia maintenance` deletes what has outlived its retention, including in idle projects:

- runs older than the plan's `retention_days` (or the project's shorter setting); an expired paid plan counts as Free
- rejected access requests 90 days after the decision
- payment orders still `created` (never paid) after 30 days

It is safe to run any number of times and prints one JSON line with the counts. Run it daily, after the backup:

```cron
45 2 * * * cd /srv/sia && sia maintenance >> /var/log/sia-maintenance.log 2>&1
```

With Docker: `docker exec sia sia maintenance`.

## Restore

1. **Stop the server.** `sia restore` refuses to run while anything answers at `SIA_BASE_URL` (default
   `http://127.0.0.1:8000`), because a running server would write over the restored file.
   ```bash
   docker stop sia            # or: systemctl stop sia
   ```
2. Restore the backup you want:
   ```bash
   sia restore /data/backups/sia-20261022-021500.db
   ```
   It checks the backup's integrity first and changes nothing if that fails. The current database is saved next to
   it as `sia-before-restore-YYYYMMDD-HHMMSS.db` before it is replaced.
3. Start the server and check it:
   ```bash
   docker start sia
   curl -fsS https://sia.example.com/health      # {"ok": true, "db": true, ...}
   ```
4. Anything written after the backup was taken is lost. Tell affected customers, and keep the `before-restore`
   file until you're sure you don't need it.

## Rotate the admin key

1. Make a new key: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Put it in `SIA_ADMIN_KEY` in the server's `.env` (or your secret store) and in wherever you run `sia admin` from.
3. Restart the server. The old key stops working immediately. Customer keys are not affected.
4. If you think the old key leaked, read the security log for `invalid_admin_key` and for admin actions you didn't
   make (`access_request_approved`, plan changes), and check `sia admin requests --status approved`.

## Revoke a leaked customer key

The customer can do it themselves: **Project & keys → Rotate** in the dashboard, or `POST /v1/keys/rotate` with
the leaked key. The old key stops working at once and their data is kept.

If they can't (the key is out and they're unreachable), revoke it on the server. Paste the leaked key at the prompt;
it is hashed and never written to disk or shell history:

```bash
python -c "import getpass, time; from sia.store import Store, hash_key; s = Store('/data/sia.db'); \
print(s.n('UPDATE api_keys SET revoked=? WHERE key_hash=? AND revoked IS NULL', time.time(), hash_key(getpass.getpass('key: '))), 'key(s) revoked')"
```

If that was their only full-scope key, make them a new one and send it over a private channel (it's shown once):

```bash
python -c "from sia.store import Store; print(Store('/data/sia.db').create_key('ORG_ID')['api_key'])"
```

## The server is down

1. **Is it the process or the network?** On the machine: `curl -sS http://127.0.0.1:8000/health`.
   - No answer: the process is down. `docker ps -a`, `docker logs --tail 200 sia`. Restart with `docker start sia`.
   - `{"ok": false, "db": false}` (503): the database can't be read. Go to step 3.
   - `{"ok": true}` locally but not from outside: check nginx (`nginx -t`, `systemctl status nginx`), the TLS
     certificate's expiry, DNS, and that `SIA_ALLOWED_HOSTS` includes the public hostname.
2. **Disk full** is the most common cause of a database error: `df -h /data`. Free space (old logs, old
   `before-restore` files), then restart.
3. **Database damaged?** With the server stopped:
   `python -c "import sqlite3; print(sqlite3.connect('/data/sia.db').execute('PRAGMA integrity_check').fetchall())"`.
   Anything but `[('ok',)]` means restore the newest good backup (above).
4. Logs are JSON lines on stdout: the `sia.access` logger has one line per request with a `request_id`, which a
   customer sees in the `X-Request-ID` header and in 500 responses. Search for it to find the stack trace.
5. When it's back, post what happened and how long it lasted to affected customers. Downtime over 24 hours in a paid
   period qualifies for a pro-rata refund (`web/refund.html`).
