#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p data/backups
exec 9>data/backups/.backup.lock
flock -n 9 || exit 0
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="data/backups/${stamp}.partial"
mkdir -p "$target"
trap 'printf "%s backup_failed %s\n" "$(date -u +%FT%TZ)" "$target" >> data/backups/status.log' ERR
docker compose exec -T db pg_dump -U postgres -d spotly -Fc > "$target/postgres.dump"
docker compose exec -T db psql -U postgres -d spotly -Atc 'SELECT original FROM photos UNION SELECT preview FROM photos UNION SELECT thumbnail FROM photos ORDER BY 1' > "$target/photo-files.txt"
tar -C data -cf "$target/photos.tar" --files-from="$PWD/$target/photo-files.txt"
cp compose.yaml Caddyfile Dockerfile pyproject.toml uv.lock "$target/"
git rev-parse HEAD > "$target/revision.txt" 2>/dev/null || printf '%s\n' "${APP_REVISION:-unknown}" > "$target/revision.txt"
(cd "$target" && sha256sum postgres.dump photos.tar > SHA256SUMS)
mv "$target" "data/backups/$stamp"
printf "%s backup_ok %s\n" "$(date -u +%FT%TZ)" "$stamp" >> data/backups/status.log
free_percent=$(df -P . | awk 'NR==2 {gsub(/%/,"",$5); print 100-$5}')
if (( free_percent < 10 )); then
    printf '%s disk_critical free=%s%%\n' "$(date -u +%FT%TZ)" "$free_percent" >> data/backups/status.log
elif (( free_percent < 20 )); then
    printf '%s disk_warning free=%s%%\n' "$(date -u +%FT%TZ)" "$free_percent" >> data/backups/status.log
fi
printf '%s\n' "data/backups/$stamp"
