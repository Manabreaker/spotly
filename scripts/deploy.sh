#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
test -f .env || { printf '%s\n' 'Create a private .env before deployment'; exit 1; }
mkdir -p data/photos data/postgres data/caddy data/backups
chmod 700 data/backups
if docker compose ps --status running --services | grep -qx db; then
    bash scripts/backup.sh
fi
docker compose config --quiet
docker compose build --pull app
docker compose up -d --wait --wait-timeout 240
