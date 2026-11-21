#!/usr/bin/env bash
# Remove o OrbitBase do servidor sem tocar nos outros sites.
# Uso: bash /opt/orbitbase/deploy/rollback.sh [--purge]
#   --purge  apaga também /opt/orbitbase (incluindo banco e arquivos processados) e o certificado
set -euo pipefail
DOMAIN="${DOMAIN:-orbitbase.biomereurb.com.br}"
APP_DIR="${APP_DIR:-/opt/orbitbase}"
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
[ "$(id -u)" -eq 0 ] || { echo "Rode como root." >&2; exit 1; }

echo "==> Parando containers"
[ -f "$APP_DIR/deploy/docker-compose.yml" ] && (cd "$APP_DIR/deploy" && docker compose down --remove-orphans) || true

echo "==> Removendo vhost do OrbitBase"
if [ -d /etc/nginx ]; then
  rm -f /etc/nginx/sites-enabled/orbitbase.conf /etc/nginx/sites-available/orbitbase.conf /etc/nginx/conf.d/orbitbase.conf
  nginx -t && systemctl reload nginx
fi
if [ -f /etc/apache2/sites-available/orbitbase.conf ] || [ -f /etc/apache2/sites-available/orbitbase-le-ssl.conf ]; then
  a2dissite -q orbitbase orbitbase-le-ssl >/dev/null 2>&1 || true
  rm -f /etc/apache2/sites-available/orbitbase.conf /etc/apache2/sites-available/orbitbase-le-ssl.conf
  apachectl configtest && systemctl reload apache2
fi

if [ "$PURGE" = "1" ]; then
  echo "==> Apagando certificado, imagens e $APP_DIR"
  command -v certbot >/dev/null && certbot delete --cert-name "$DOMAIN" --non-interactive 2>/dev/null || true
  docker image rm orbitbase-backend:latest 2>/dev/null || true
  rm -rf "$APP_DIR"
else
  echo "Dados mantidos em $APP_DIR/deploy/data (use --purge para apagar)."
fi
echo "Pronto. Os demais sites não foram alterados. Backups da config em /root/orbitbase-backups/."
