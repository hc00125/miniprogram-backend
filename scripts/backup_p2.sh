#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/touchi}"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
MEDIA_ROOT="${MEDIA_ROOT:-$PROJECT_ROOT/media}"
STAMP="$(date +%Y%m%d_%H%M%S)"
TARGET="$BACKUP_ROOT/$STAMP"

mkdir -p "$TARGET"
chmod 700 "$TARGET"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL 未设置，停止备份" >&2
  exit 2
fi

printf '%s\n' "$(git -C "$PROJECT_ROOT" rev-parse HEAD)" > "$TARGET/git-head.txt"
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" > "$TARGET/database.dump"

if [[ -d "$MEDIA_ROOT" ]]; then
  tar -C "$(dirname "$MEDIA_ROOT")" -czf "$TARGET/media.tar.gz" "$(basename "$MEDIA_ROOT")"
fi

for path in \
  "$PROJECT_ROOT/.env" \
  "/etc/nginx" \
  "/etc/systemd/system"; do
  if [[ -e "$path" ]]; then
    name="$(echo "$path" | sed 's#^/##;s#/#_#g')"
    tar -czf "$TARGET/${name}.tar.gz" "$path" 2>/dev/null || true
  fi
done

(
  cd "$TARGET"
  sha256sum ./* > SHA256SUMS
)

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +14 -print0 | xargs -0r rm -rf

echo "P2备份完成：$TARGET"
