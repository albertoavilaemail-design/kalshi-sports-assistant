#!/usr/bin/env bash
# Persist the SQLite database between GitHub Actions runs on a dedicated branch.
#   scripts/state.sh pull   -> restore data/assistant.db from the state branch (if it exists)
#   scripts/state.sh push   -> save data/assistant.db to the state branch (single orphan commit, force-pushed)
# Runs are serialized by the workflow `concurrency` group, so force-pushing is safe.
set -euo pipefail
MODE="${1:?usage: state.sh pull|push}"
BRANCH="${STATE_BRANCH:-bot-state}"
DB="${KSA_DB_PATH:-data/assistant.db}"
mkdir -p "$(dirname "$DB")"

if [ "$MODE" = "pull" ]; then
  if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    git fetch --quiet --depth=1 origin "$BRANCH"
    git show "FETCH_HEAD:assistant.db" > "$DB"
    echo "Restored state from branch $BRANCH ($(wc -c < "$DB") bytes)"
  else
    echo "No state branch yet; starting fresh."
  fi
elif [ "$MODE" = "push" ]; then
  [ -f "$DB" ] || { echo "No database to save."; exit 0; }
  python - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()
PY
  export GIT_AUTHOR_NAME="state-bot" GIT_AUTHOR_EMAIL="state-bot@users.noreply.github.com"
  export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
  BLOB=$(git hash-object -w "$DB")
  TREE=$(printf '100644 blob %s\tassistant.db\n' "$BLOB" | git mktree)
  COMMIT=$(git commit-tree "$TREE" -m "state $(date -u +%Y-%m-%dT%H:%M:%SZ)")
  git push --quiet --force origin "$COMMIT:refs/heads/$BRANCH"
  echo "Saved state to branch $BRANCH"
else
  echo "unknown mode $MODE"; exit 1
fi
