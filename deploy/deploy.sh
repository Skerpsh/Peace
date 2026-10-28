#!/usr/bin/env bash
# Deploys PEACE on the server: pulls the code, backs up the database,
# installs dependencies, runs the tests, restarts the bot, checks it stays
# up, and releases the deployed commit (the next version's tag).
#
#   deploy/deploy.sh                 # everything
#   deploy/deploy.sh --no-backup     # skip the database backup
#   deploy/deploy.sh --no-release    # no new version tag
#
# Paths and names can be changed with the variables below.
set -euo pipefail

# Everything runs inside main(), which bash reads completely before running
# it: `git pull` below may replace this file, and bash would otherwise go on
# reading the new version from the old position.
main() {

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SERVICE="${SERVICE:-peace-bot}"
# Seconds the bot must stay up after the restart.
UP_SECONDS="${UP_SECONDS:-10}"
# The release (below): a fine-grained GitHub token of the repository with
# "Contents: Read and write", in a file only root reads.
RELEASE_TOKEN_FILE="${RELEASE_TOKEN_FILE:-/etc/peace/release-token}"
GITHUB_REPO="${GITHUB_REPO:-Skerpsh/Peace}"

backup=1
release=1

for arg in "$@"; do
  case "$arg" in
    --no-backup) backup=0 ;;
    --no-release) release=0 ;;
    -h | --help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    *)
      echo "deploy: unknown option $arg (see --help)" >&2
      exit 1
      ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }

cd "$REPO"

step "Pulling the code"
git pull --ff-only

if [ "$backup" = 1 ]; then
  step "Backing up the database"
  "$REPO/deploy/backup.sh"
fi

step "Installing dependencies"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

step "Running the tests"
.venv/bin/python -m pytest -q -p no:cacheprovider

# New settings in .env.example that the server's .env does not have yet.
missing="$(comm -23 <(grep -oE '^[A-Z_]+=' .env.example | sort -u) \
                    <(grep -oE '^[A-Z_]+=' .env 2>/dev/null | sort -u) | tr -d '=' | xargs)"
if [ -n "$missing" ]; then
  echo "Not set in .env (defaults used): $missing — see .env.example"
fi

step "Restarting $SERVICE"
systemctl restart "$SERVICE"
before="$(systemctl show -p NRestarts --value "$SERVICE")"

step "Checking that $SERVICE stays up"
sleep "$UP_SECONDS"
if ! systemctl is-active --quiet "$SERVICE" \
  || [ "$(systemctl show -p NRestarts --value "$SERVICE")" != "$before" ]; then
  echo "deploy: $SERVICE does not stay up; last log lines:" >&2
  journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
  exit 1
fi
echo "$SERVICE is up"

# The next version's tag on what was deployed; release.yml turns it into a
# GitHub release with the list of changes. Made through GitHub's API, so the
# server needs no key to push; a failure does not undo the deploy.
if [ "$release" = 1 ] && [ -r "$RELEASE_TOKEN_FILE" ]; then
  step "Releasing"
  commit=$(git rev-parse HEAD)
  git fetch --tags --quiet origin || true

  if git tag --points-at "$commit" | grep -q '^v'; then
    echo "This commit is released already ($(git tag --points-at "$commit" | grep '^v' | head -1))"
  else
    last=$(git tag -l 'v*' --sort=-v:refname | head -1)
    IFS=. read -r major minor patch <<<"${last:-v1.0.0}"
    next="${major}.${minor}.$((patch + 1))"
    [ -n "$last" ] || next="v1.0.0"

    if curl -fsS -o /dev/null -X POST \
      -H "Authorization: Bearer $(cat "$RELEASE_TOKEN_FILE")" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$GITHUB_REPO/git/refs" \
      -d "{\"ref\": \"refs/tags/$next\", \"sha\": \"$commit\"}"; then
      echo "Tagged $next: GitHub Actions publishes its release notes"
      git fetch --tags --quiet origin || true
    else
      echo "deploy: could not tag $next (is the token in $RELEASE_TOKEN_FILE valid?); the deploy itself is done" >&2
    fi
  fi
elif [ "$release" = 1 ]; then
  echo
  echo "No $RELEASE_TOKEN_FILE: nothing is released (see deploy/README.md)"
fi

step "Done: $(git describe --tags --always)"

}

main "$@"
