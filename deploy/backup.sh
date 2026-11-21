#!/usr/bin/env bash
# Backup diário do OrbitBase: cópia consistente do SQLite + limpeza de processamentos antigos.
# Instalado pelo cron em /etc/cron.d/orbitbase-backup.
#   KEEP_DAYS  dias de backups do banco mantidos (padrão 14)
#   PROC_DAYS  apaga pastas de storage/processing sem alteração há N dias (padrão 30; 0 desliga)
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/orbitbase}"
DEST="${DEST:-/root/orbitbase-backups/db}"
KEEP_DAYS="${KEEP_DAYS:-14}"
PROC_DAYS="${PROC_DAYS:-30}"
DATA="$APP_DIR/deploy/data"
STAMP="$(date +%Y%m%d-%H%M)"

mkdir -p "$DEST"
# Usa a API de backup do SQLite dentro do container (cópia consistente mesmo com o app rodando).
docker exec orbitbase-backend-1 python -c "
import sqlite3
src = sqlite3.connect('/app/backend/data/orbitbase.db')
dst = sqlite3.connect('/app/backend/data/.backup-tmp.db')
src.backup(dst); dst.close(); src.close()
"
mv "$DATA/db/.backup-tmp.db" "$DEST/orbitbase-$STAMP.db"
gzip -f "$DEST/orbitbase-$STAMP.db"
find "$DEST" -name 'orbitbase-*.db.gz' -mtime +"$KEEP_DAYS" -delete

if [ "$PROC_DAYS" -gt 0 ] && [ -d "$DATA/storage/processing" ]; then
  find "$DATA/storage/processing" -mindepth 1 -maxdepth 1 -type d -mtime +"$PROC_DAYS" -exec rm -rf {} +
fi
echo "$(date -Is) backup ok: $DEST/orbitbase-$STAMP.db.gz ($(du -sh "$DATA/storage" | cut -f1) em storage)"
