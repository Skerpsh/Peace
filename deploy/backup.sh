#!/usr/bin/env bash
# Backs up the PEACE database (SQLite: subscribers, settings, event history)
# as a compressed copy and keeps only the newest KEEP backups. Skips the
# backup when the disk is nearly full.
#
#   deploy/backup.sh                       # with the defaults below
#   KEEP=3 BACKUP_DIR=/srv/backups deploy/backup.sh
#
# Run daily by deploy/peace-backup.timer (see deploy/README.md).
# Restore: systemctl stop peace-bot; gunzip -c <file> > data/peace.db; systemctl start peace-bot
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/peace}"
KEEP="${KEEP:-7}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
# Space that must stay free on the disk besides the new backup.
MIN_FREE_MB="${MIN_FREE_MB:-200}"
# Optional: a Healthchecks.io ping URL (see deploy/README.md). A backup pings
# it, a failure pings its /fail, and no ping for a day raises an alarm.
HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"
# Optional: a copy off the server (see deploy/README.md, "Off-site
# backups"). OFFSITE_REMOTE is an rclone path such as b2:my-bucket, and
# AGE_RECIPIENT the age public key the copy is encrypted for.
OFFSITE_REMOTE="${OFFSITE_REMOTE:-}"
AGE_RECIPIENT="${AGE_RECIPIENT:-}"
OFFSITE_KEEP_DAYS="${OFFSITE_KEEP_DAYS:-30}"

ping_healthcheck() {
  if [ -n "$HEALTHCHECK_URL" ]; then
    curl -fsS -m 10 --retry 3 -o /dev/null "$HEALTHCHECK_URL$1" || true
  fi
}

# Any failing command reports the failure.
trap 'ping_healthcheck /fail' ERR

fail() {
  echo "backup: $*" >&2
  ping_healthcheck /fail
  exit 1
}

# Reads KEY=VALUE from .env without running it as a script.
env_value() {
  local line value
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  value="${line#*=}"
  value="${value%$'\r'}"
  if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
    value="${BASH_REMATCH[1]}"
  fi
  printf '%s' "$value"
}

DB="$(env_value DB_PATH)"
DB="${DB:-data/peace.db}"
[[ "$DB" = /* ]] || DB="$REPO/$DB"

if [ ! -f "$DB" ]; then
  fail "no database at $DB"
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

db_bytes="$(stat -c %s "$DB")"
free_bytes="$(df --output=avail -B1 "$BACKUP_DIR" | tail -n 1 | tr -d ' ')"
needed_bytes=$((db_bytes + MIN_FREE_MB * 1024 * 1024))

if [ "$free_bytes" -lt "$needed_bytes" ]; then
  fail "skipped, not enough disk space in $BACKUP_DIR" \
    "($((free_bytes / 1024 / 1024)) MB free, need $((needed_bytes / 1024 / 1024)) MB)"
fi

file="$BACKUP_DIR/peace-$(date +%Y%m%d-%H%M%S).db.gz"

# SQLite's online backup: a consistent copy while the bot keeps writing.
# Written under a temporary name, so a failed backup never looks complete.
python3 - "$DB" "$file.db" <<'EOF'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
EOF
gzip -c "$file.db" > "$file.partial"
rm -f "$file.db"
mv "$file.partial" "$file"
chmod 600 "$file"

# Keep the newest KEEP backups.
find "$BACKUP_DIR" -maxdepth 1 -name 'peace-*.db.gz' -printf '%T@ %p\n' \
  | sort -rn \
  | tail -n +"$((KEEP + 1))" \
  | cut -d' ' -f2- \
  | xargs -r rm --

# The copy leaves encrypted for a key whose private half is not on this server.
if [ -n "$OFFSITE_REMOTE" ]; then
  if [ -z "$AGE_RECIPIENT" ]; then
    fail "OFFSITE_REMOTE is set but AGE_RECIPIENT is not"
  fi
  age -r "$AGE_RECIPIENT" "$file" | rclone rcat "$OFFSITE_REMOTE/db/$(basename "$file").age"
  rclone delete --min-age "${OFFSITE_KEEP_DAYS}d" "$OFFSITE_REMOTE/db"
  echo "backup: copied to $OFFSITE_REMOTE"
fi

count="$(find "$BACKUP_DIR" -maxdepth 1 -name 'peace-*.db.gz' | wc -l)"
echo "backup: $file ($(du -h "$file" | cut -f1)), $count kept in $BACKUP_DIR"
ping_healthcheck ""
