#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p data/backups data/photos
exec 9>data/backups/.backup.lock
flock -n 9 || exit 0
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="data/backups/${stamp}.partial"
mkdir -p "$target"
trap 'printf "%s backup_failed %s\n" "$(date -u +%FT%TZ)" "$target" >> data/backups/status.log' ERR
# Keep pending originals and published variants stable until tar has read them.
exec 8< data/photos
flock -x 8
docker compose exec -T db pg_dump -U postgres -d spotly -Fc > "$target/postgres.dump"
photo_files_sql='SELECT original FROM photos UNION SELECT preview FROM photos UNION SELECT thumbnail FROM photos'
if [[ $(docker compose exec -T db psql -U postgres -d spotly -Atc "SELECT to_regclass('photo_jobs') IS NOT NULL") == t ]]; then
    photo_files_sql+=" UNION SELECT original FROM photo_jobs WHERE status IN ('queued', 'processing')"
fi
docker compose exec -T db psql -U postgres -d spotly -Atc "$photo_files_sql ORDER BY 1" > "$target/photo-files.txt"
tar -C data -cf "$target/photos.tar" --files-from="$PWD/$target/photo-files.txt"
exec 8<&-
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
backup_cutoff=$(date -u -d '7 days ago' +%Y%m%dT%H%M%SZ)
for backup in data/backups/*; do
    backup_stamp=${backup##*/}
    if [[ -d "$backup" && ! -L "$backup" &&
          "$backup_stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ &&
          "$backup_stamp" < "$backup_cutoff" ]]; then
        rm -rf -- "$backup"
    fi
done
printf '%s\n' "data/backups/$stamp"
