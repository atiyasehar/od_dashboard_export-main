#!/usr/bin/env bash
# Start the OD dashboard using deploy.env from the project root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/deploy.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -v '^#' "$ENV_FILE" | grep -v '^[[:space:]]*$' | sed 's/^/export /')
  set +a
  echo "Loaded $ENV_FILE"
else
  echo "No deploy.env found — copy deploy.example.env to deploy.env and edit PostgreSQL settings."
fi

exec python scripts/run_dashboard.py --bundle-root . "$@"
