#!/usr/bin/env bash
# Remove o OrbitBase do servidor sem tocar nos outros sites.
# Uso: bash /opt/orbitbase/deploy/rollback.sh [--purge]
#   --purge  apaga também /opt/orbitbase (incluindo banco e arquivos processados) e o certificado
set -euo pipefail
DOMAIN="${DOMAIN:-orbitbase.biomereurb.com.br}"
APP_DIR="${APP_DIR:-/opt/orbitbase}"
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
[ "$(id -u)" -eq 0 ] || { echo "Rode como root." >&2; exit 1; }

echo "==> Removendo bloco do OrbitBase do Caddy (se houver)"
CADDY_ENV="$(ls -1d /root/orbitbase-backups/*/caddy.env 2>/dev/null | tail -1 || true)"
if [ -n "$CADDY_ENV" ]; then
  # shellcheck disable=SC1090
  . "$CADDY_ENV"
  if [ -f "$CADDYFILE" ] && grep -q '^# >>> orbitbase' "$CADDYFILE"; then
    cp -p "$CADDYFILE" "$CADDYFILE.antes-rollback-orbitbase"
    awk '/^# >>> orbitbase/{skip=1} !skip{print} /^# <<< orbitbase/{skip=0}' "$CADDYFILE.antes-rollback-orbitbase" > /tmp/Caddyfile.orbitbase-rollback
    cat /tmp/Caddyfile.orbitbase-rollback > "$CADDYFILE"   # mesmo inode (bind mount)
    rm -f /tmp/Caddyfile.orbitbase-rollback
    docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  fi
fi

echo "==> Parando containers"
[ -f "$APP_DIR/deploy/docker-compose.yml" ] && (cd "$APP_DIR/deploy" && docker compose -p orbitbase down --remove-orphans) || true

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
