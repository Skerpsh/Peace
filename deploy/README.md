# PEACE server operations

Scripts for the VPS. They assume this layout:

| What | Where |
|---|---|
| Repository | `/opt/Peace` |
| Bot service | `systemd` unit `peace-bot` |
| Settings | `/opt/Peace/.env` |
| Database (SQLite) | `/opt/Peace/data/peace.db` |
| Backups | `/var/backups/peace` |

Each script lists its variables at the top if anything is different. The
commands below run as root.

## First install

On a fresh Ubuntu/Debian server, one command per step:

1. Packages and the code (the repository is private: add the server's key on
   GitHub, Settings → Deploy keys, read-only; `ssh-keygen -t ed25519` makes one,
   `cat ~/.ssh/id_ed25519.pub` shows it):

```bash
apt-get install -y git python3 python3-venv curl && git clone git@github.com:Skerpsh/Peace.git /opt/Peace
```

2. Settings: `BOT_TOKEN` (from [@BotFather](https://t.me/BotFather)) is
   required; `ALERTS_API_TOKEN` is optional; `ADMIN_CHAT_ID` comes in step 5:

```bash
cd /opt/Peace && cp .env.example .env && chmod 600 .env && nano .env
```

3. The service and the daily backup:

```bash
cp /opt/Peace/deploy/peace-bot.service /opt/Peace/deploy/peace-backup.service /opt/Peace/deploy/peace-backup.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl enable peace-bot && systemctl enable --now peace-backup.timer
```

4. The first deploy (there is no database to back up yet):

```bash
/opt/Peace/deploy/deploy.sh --no-backup
```

5. Send `/id` to the bot, put the number (instead of `ID`) into `.env`:

```bash
sed -i 's/^ADMIN_CHAT_ID=.*/ADMIN_CHAT_ID=ID/' /opt/Peace/.env && systemctl restart peace-bot
```

The bot writes "PEACE запущено" with a first report.

Optional, for faster updates than the 5-second web poller: a Telethon
userbot session (phone + code, once):

```bash
cd /opt/Peace && PYTHONPATH=src .venv/bin/python -m peace.login && systemctl restart peace-bot
```

### Moving from the old scripts

A server set up with the old `./deploy.sh` / `setup_vps.sh` (repository in
`/opt/Peace`) needs the new units once:

The repository there belongs to the old service user, and the scripts now run
as root, so git has to be told it is safe:

```bash
git config --global --add safe.directory /opt/Peace && cd /opt/Peace && git pull --ff-only && cp deploy/peace-bot.service deploy/peace-backup.service deploy/peace-backup.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now peace-backup.timer && deploy/deploy.sh
```

`/etc/sudoers.d/peace-bot` from the old setup is no longer needed (`rm` it).

## Deploy

```bash
cd /opt/Peace
deploy/deploy.sh                 # everything
deploy/deploy.sh --no-backup     # skip the database backup
deploy/deploy.sh --no-release    # no new version tag
```

The script stops at the first error, before the bot is restarted if the
pull, the backup, the dependencies or the tests fail. After the restart it
checks that the bot stays up for 10 seconds without systemd restarting it,
and prints the service log if it does not. It also lists settings from
`.env.example` that the server's `.env` does not have yet.

Logs: `journalctl -u peace-bot -f`. State: `systemctl status peace-bot`.

## Releases

At its end the deploy tags the deployed commit with the next version
(v1.0.1 → v1.0.2); [`release.yml`](../.github/workflows/release.yml) turns
the tag into a GitHub release listing the commits since the previous
version. Nothing is tagged if the commit has a version already. `/stats`
shows the running version.

It tags through GitHub's API with a token, so the server needs no key to
push. Once: on GitHub, Settings → Developer settings → Fine-grained tokens →
Generate, only this repository, **Contents: Read and write**; then on the
server:

```bash
install -d -m 700 /etc/peace && sh -c 'read -r t && printf %s "$t" > /etc/peace/release-token' && chmod 600 /etc/peace/release-token
```

(paste the token and press Enter). Without the file the deploy says so and
releases nothing.

Every push and pull request also runs the tests on GitHub
([`ci.yml`](../.github/workflows/ci.yml)).

## Monitoring

The bot itself reports to `ADMIN_CHAT_ID` (see *First install*, step 5):

- **Every 15 minutes** (at :00, :15, :30, :45), silently: subscribers (▲/▼
  since the previous report) and how many use night mode, active zones,
  events, unrecognised messages, pushes and failed sends since the previous
  report, sources working, disk, RAM, load, the bot's memory and uptime, the
  last backup.
- **At midnight** (Kyiv), the day compared with the day before:
  subscribers, events, unrecognised messages, pushes.
- **Alarms** with sound, and again when fixed: a source (channel or the
  official API) failing for 10 minutes; checked every minute and raised
  after two checks in a row: the disk below 10% or 1 GB, free memory below
  5%, the newest backup older than 26 hours.
- **Commands** (only in the admin chat): `/stats` (the report now), `/today`
  (the day so far), `/health` (current problems), `/debug` (recent messages
  the parser ignored), `/debug <text>` (how a text is parsed).

Settings in `.env` (the defaults are shown):

| Variable | Default | |
|---|---|---|
| `MONITOR_REPORT_MINUTES` | `15` | how often to report |
| `MONITOR_CHECK_SECONDS` | `60` | how often to check |
| `BACKUP_DIR` | `/var/backups/peace` | where the monitor looks for backups |
| `BACKUP_MAX_AGE_HOURS` | `26` | alarm when the newest backup is older |

If the whole server is down, the bot is down too and says nothing: the
Healthchecks.io check below then alarms within a day.

## Backups

`deploy/backup.sh` copies the database (SQLite's online backup, consistent
while the bot runs), compresses it to `/var/backups/peace` and keeps the
newest 7. It fails without writing when the disk would have less than
200 MB left. The deploy runs it before every update; the timer daily at
04:00. By hand:

```bash
deploy/backup.sh
KEEP=3 deploy/backup.sh

systemctl list-timers peace-backup.timer   # next run
systemctl start peace-backup.service       # run one now
journalctl -u peace-backup -n 20           # what happened
ls -lh /var/backups/peace
```

The database holds the subscribers, their zones and settings, and the
history of events. The Telethon session (`data/*.session`) is not backed
up: it is a login to the Telegram account, and `peace.login` makes a new one.

### Restore

```bash
systemctl stop peace-bot
gunzip -c /var/backups/peace/peace-YYYYMMDD-HHMMSS.db.gz > /opt/Peace/data/peace.db
systemctl start peace-bot
```

### Healthchecks.io

1. Sign up at <https://healthchecks.io>, *Integrations* → *Telegram* →
   follow the link to their bot.
2. *Add check*: name "PEACE backup", period **1 day**, grace **2 hours**.
   Copy its ping URL (`https://hc-ping.com/…`).
3. On the server (put your URL in):

```bash
mkdir -p /etc/systemd/system/peace-backup.service.d && printf '[Service]\nEnvironment=HEALTHCHECK_URL=https://hc-ping.com/YOUR-UUID\n' > /etc/systemd/system/peace-backup.service.d/healthcheck.conf && systemctl daemon-reload && systemctl start peace-backup.service
```

A failed backup alarms right away, and one that does not run at all (the
server is down) alarms after a day and two hours.

### Off-site backups

The backups above are on the same disk as the database: they protect against
mistakes, not against losing the server. With the off-site copy on, every
daily backup also goes to a bucket elsewhere (Backblaze B2 here; any
[rclone](https://rclone.org) remote works), encrypted with
[age](https://age-encryption.org) for a key whose private half never touches
the server. Copies older than 30 days are deleted (`OFFSITE_KEEP_DAYS`).

1. In Backblaze: *Create a Bucket* (private). *Application Keys* → *Add a
   New Application Key* for that bucket only, read and write. Note the keyID
   and applicationKey (shown once).
2. On your computer (not the server), make the key and keep the file in your
   password manager; it prints the public key, `age1…`:

```bash
age-keygen -o peace-backup.key
```

3. On the server (put your values in):

```bash
apt-get install -y rclone age && mkdir -p /etc/peace && printf 'RCLONE_CONFIG_B2_TYPE=b2\nRCLONE_CONFIG_B2_ACCOUNT=KEY_ID\nRCLONE_CONFIG_B2_KEY=APPLICATION_KEY\nOFFSITE_REMOTE=b2:BUCKET\nAGE_RECIPIENT=age1...\n' > /etc/peace/backup.env && chmod 600 /etc/peace/backup.env && systemctl start peace-backup.service && journalctl -u peace-backup -n 20 --no-pager
```

The log ends with `copied to b2:BUCKET`. Backups run by `deploy.sh` stay on
the server; the daily one goes off-site.

Restore from the bucket, on any machine with rclone, age and the key:

```bash
rclone copy b2:BUCKET/db/peace-YYYYMMDD-HHMMSS.db.gz.age . && age -d -i peace-backup.key peace-YYYYMMDD-HHMMSS.db.gz.age | gunzip > peace.db
```
