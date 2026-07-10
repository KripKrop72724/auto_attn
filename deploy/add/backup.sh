#!/bin/sh
set -eu

umask 077
backup_dir="${ADD_BACKUP_DIR:-./backups/add}"
retention_days="${ADD_BACKUP_RETENTION_DAYS:-14}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"

docker compose --env-file .env.add -f docker-compose.add.yml exec -T postgres \
  pg_dump -Fc -U "${ADD_POSTGRES_USER:-add_service}" "${ADD_POSTGRES_DB:-attendance_devices}" \
  > "$backup_dir/attendance-devices-$stamp.dump"

find "$backup_dir" -type f -name 'attendance-devices-*.dump' -mtime "+$retention_days" -delete
