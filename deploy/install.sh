#!/usr/bin/env bash
# Instalação idempotente do OrbitBase numa VPS Ubuntu que já hospeda outro site.
# Uso (como root):
#   curl -fsSL https://raw.githubusercontent.com/Potyguara/orbitbase/<branch>/deploy/install.sh | \
#     DOMAIN=orbitbase.biomereurb.com.br EMAIL=voce@exemplo.com bash
# Variáveis:
#   DOMAIN   subdomínio público (padrão orbitbase.biomereurb.com.br)
#   EMAIL    e-mail para o Let's Encrypt (opcional)
#   BRANCH   branch do repositório (padrão main)
#   APP_DIR  pasta da instalação (padrão /opt/orbitbase)
#   PORT     porta local do container web (padrão 8088)
#   MODE     "check" só inspeciona o servidor e não altera nada
#   SKIP_TLS=1 pula o certbot (ex.: DNS ainda não propagou)
set -euo pipefail

DOMAIN="${DOMAIN:-orbitbase.biomereurb.com.br}"
EMAIL="${EMAIL:-}"
REPO_URL="${REPO_URL:-https://github.com/Potyguara/orbitbase.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/orbitbase}"
PORT="${PORT:-8088}"
MODE="${MODE:-install}"
SKIP_TLS="${SKIP_TLS:-0}"
BACKUP_DIR="/root/orbitbase-backups/$(date +%Y%m%d-%H%M%S)"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[aviso] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[erro] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Rode como root."

# ---------------------------------------------------------------- detecção
detect_web_server() {
  local owners
  owners="$(ss -ltnpH '( sport = :80 or sport = :443 )' 2>/dev/null | grep -o 'users:(("[^"]*"' | cut -d'"' -f2 | sort -u | tr '\n' ' ')"
  case "$owners" in
    *nginx*)               echo nginx ;;
    *apache2*|*httpd*)     echo apache ;;
    *litespeed*|*lshttpd*|*openlitespeed*) echo litespeed ;;
    *docker-proxy*)
      if docker ps --filter publish=443 --format '{{.Image}}' 2>/dev/null | grep -qi caddy; then echo caddy-docker; else echo docker; fi ;;
    *traefik*|*caddy*)     echo other:$owners ;;
    "")                    echo none ;;
    *)                     echo "other:$owners" ;;
  esac
}

public_ip() { curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}'; }
dns_ip()    { getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}'; }

report() {
  local -; set +e +o pipefail
  log "Diagnóstico do servidor"
  echo "Sistema:      $(. /etc/os-release; echo "$PRETTY_NAME")"
  echo "CPU:          $(nproc) vCPU | load: $(cut -d' ' -f1-3 /proc/loadavg)"
  free -h | awk 'NR<=3'
  df -h / | awk 'NR<=2'
  echo "Swap ativo:   $(swapon --show --noheadings | wc -l) dispositivo(s)"
  echo "Servidor web: $(detect_web_server)"
  echo "Portas 80/443:"; ss -ltnp '( sport = :80 or sport = :443 )' 2>/dev/null | sed 's/^/  /'
  echo "Docker:       $(command -v docker >/dev/null && docker --version || echo 'não instalado')"
  if command -v docker >/dev/null; then docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null || true; fi
  echo "Porta $PORT:    $(ss -ltnH "( sport = :$PORT )" | grep -q . && echo 'EM USO' || echo 'livre')"
  echo "IP público:   $(public_ip)"
  echo "DNS $DOMAIN -> $(dns_ip || true)"
  [ -d /etc/nginx ] && { echo "Sites nginx:"; ls /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | sed 's/^/  /'; }
  [ -d /etc/apache2 ] && { echo "Sites apache:"; ls /etc/apache2/sites-enabled 2>/dev/null | sed 's/^/  /'; }
  command -v certbot >/dev/null && { echo "Certificados:"; certbot certificates 2>/dev/null | grep -E 'Certificate Name|Domains|Expiry' | sed 's/^/  /'; }
  [ -d "$APP_DIR" ] && echo "OrbitBase já existe em $APP_DIR" || true
}

if [ "$MODE" = "check" ]; then report; exit 0; fi

report
WEB="$(detect_web_server)"
case "$WEB" in
  nginx|apache|none|caddy-docker) ;;
  *) die "Servidor web '$WEB' nas portas 80/443 não é configurado automaticamente. O OrbitBase pode subir em 127.0.0.1:$PORT; configure o proxy manualmente (veja deploy/README.md)." ;;
esac
ss -ltnpH "( sport = :$PORT )" | grep -q . && ! ss -ltnpH "( sport = :$PORT )" | grep -q docker \
  && die "A porta $PORT já está em uso por outro processo. Rode com PORT=<outra>."

# ---------------------------------------------------------------- backup
log "Backup da configuração atual em $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
if [ "$WEB" = "caddy-docker" ]; then
  CADDY_CONTAINER="$(docker ps --filter publish=443 --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /caddy/ {print $1; exit}')"
  CADDYFILE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' "$CADDY_CONTAINER")"
  if [ -z "$CADDYFILE" ]; then
    CADDYDIR="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy"}}{{.Source}}{{end}}{{end}}' "$CADDY_CONTAINER")"
    [ -n "$CADDYDIR" ] && CADDYFILE="$CADDYDIR/Caddyfile"
  fi
  [ -n "$CADDYFILE" ] && [ -f "$CADDYFILE" ] || die "Não achei o Caddyfile montado no container $CADDY_CONTAINER."
  CADDY_NETWORK="${CADDY_NETWORK:-$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CADDY_CONTAINER" | awk '{print $1}')}"
  echo "Caddy: container=$CADDY_CONTAINER caddyfile=$CADDYFILE rede=$CADDY_NETWORK"
  cp -p "$CADDYFILE" "$BACKUP_DIR/Caddyfile"
  printf 'CADDY_CONTAINER=%s\nCADDYFILE=%s\nCADDY_NETWORK=%s\n' "$CADDY_CONTAINER" "$CADDYFILE" "$CADDY_NETWORK" > "$BACKUP_DIR/caddy.env"
fi
for d in /etc/nginx /etc/apache2 /etc/letsencrypt; do
  [ -d "$d" ] && tar -czf "$BACKUP_DIR/$(basename "$d").tgz" -C / "${d#/}" 2>/dev/null || true
done
echo "$WEB" > "$BACKUP_DIR/web_server.txt"

# ---------------------------------------------------------------- pacotes
log "Pacotes base"
export DEBIAN_FRONTEND=noninteractive
need=()
for p in git curl ca-certificates openssl; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
if ! command -v docker >/dev/null; then need+=(docker.io docker-compose-v2); fi
if command -v docker >/dev/null && ! docker compose version >/dev/null 2>&1; then need+=(docker-compose-v2); fi
if [ ${#need[@]} -gt 0 ]; then apt-get update -qq; apt-get install -y -qq "${need[@]}"; fi
systemctl enable --now docker >/dev/null 2>&1 || true

# ---------------------------------------------------------------- código
log "Código em $APP_DIR (branch $BRANCH)"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q -f -B "$BRANCH" FETCH_HEAD
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR/deploy"
mkdir -p data/storage data/db
chown -R 1000:1000 data

if [ ! -f .env ]; then
  log "Criando deploy/.env com SECRET_KEY aleatória"
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env
  chmod 600 .env
fi
sed -i "s|^CORS_ORIGINS=.*|CORS_ORIGINS=https://$DOMAIN|" .env
grep -q '^ORBITBASE_PORT=' .env && sed -i "s|^ORBITBASE_PORT=.*|ORBITBASE_PORT=$PORT|" .env || echo "ORBITBASE_PORT=$PORT" >> .env

log "Subindo containers"
COMPOSE=(docker compose --env-file .env -f docker-compose.yml)
if [ "$WEB" = "caddy-docker" ]; then
  grep -q '^CADDY_NETWORK=' .env && sed -i "s|^CADDY_NETWORK=.*|CADDY_NETWORK=$CADDY_NETWORK|" .env || echo "CADDY_NETWORK=$CADDY_NETWORK" >> .env
  COMPOSE+=(-f docker-compose.caddy.yml)
fi
"${COMPOSE[@]}" up -d --build --remove-orphans
# Confere o conteúdo, não só o HTTP 200: garante que /api chega ao backend do OrbitBase.
api_ok() { curl -fsS "http://127.0.0.1:$PORT/api/" 2>/dev/null | grep -q '"OrbitBase API"'; }
for i in $(seq 1 30); do api_ok && break; sleep 2; done
api_ok || die "/api em 127.0.0.1:$PORT não respondeu como OrbitBase API (veja: docker compose logs backend web)"
echo "OK: http://127.0.0.1:$PORT/api/ -> OrbitBase API"

# ---------------------------------------------------------------- proxy do host
PROXY_BLOCK="
    client_max_body_size 1024m;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_request_buffering off;
    }"

setup_nginx() {
  dpkg -s nginx >/dev/null 2>&1 || command -v nginx >/dev/null || apt-get install -y -qq nginx
  local avail=/etc/nginx/sites-available/orbitbase.conf enabled=/etc/nginx/sites-enabled/orbitbase.conf
  if [ ! -d /etc/nginx/sites-enabled ]; then avail=/etc/nginx/conf.d/orbitbase.conf; enabled=""; fi
  if [ -f "$avail" ] && grep -q "managed-by-certbot\|ssl_certificate" "$avail"; then
    echo "Vhost $avail já tem HTTPS; mantendo."
  else
    printf 'server {\n    listen 80;\n    listen [::]:80;\n    server_name %s;\n%s\n}\n' "$DOMAIN" "$PROXY_BLOCK" > "$avail"
  fi
  [ -n "$enabled" ] && ln -sf "$avail" "$enabled"
  if ! nginx -t; then
    rm -f "$avail" ${enabled:+"$enabled"}
    die "nginx -t falhou; vhost do OrbitBase removido, site existente intacto."
  fi
  systemctl reload nginx
}

setup_apache() {
  a2enmod -q proxy proxy_http headers >/dev/null
  local conf=/etc/apache2/sites-available/orbitbase.conf
  if [ ! -f "$conf" ]; then
    cat > "$conf" <<APACHE
<VirtualHost *:80>
    ServerName $DOMAIN
    ProxyPreserveHost On
    ProxyTimeout 3600
    RequestHeader set X-Forwarded-Proto "http"
    ProxyPass / http://127.0.0.1:$PORT/
    ProxyPassReverse / http://127.0.0.1:$PORT/
    LimitRequestBody 1073741824
</VirtualHost>
APACHE
  fi
  a2ensite -q orbitbase >/dev/null
  if ! apachectl configtest; then
    a2dissite -q orbitbase >/dev/null; rm -f "$conf"
    die "apachectl configtest falhou; vhost do OrbitBase removido, site existente intacto."
  fi
  systemctl reload apache2
}

setup_caddy_docker() {
  local begin="# >>> orbitbase (gerenciado por /opt/orbitbase/deploy/install.sh)" end="# <<< orbitbase"
  if grep -qF "$begin" "$CADDYFILE"; then
    echo "Bloco do OrbitBase já existe no Caddyfile; mantendo."
  else
    # Acrescenta no mesmo arquivo (mesmo inode), para o bind mount do container enxergar a mudança.
    printf '\n%s\n%s {\n\tencode gzip\n\trequest_body {\n\t\tmax_size 1GB\n\t}\n\treverse_proxy orbitbase-web:80 {\n\t\ttransport http {\n\t\t\tread_timeout 1h\n\t\t\twrite_timeout 1h\n\t\t}\n\t}\n}\n%s\n' \
      "$begin" "$DOMAIN" "$end" >> "$CADDYFILE"
  fi
  if ! docker exec "$CADDY_CONTAINER" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
    cat "$BACKUP_DIR/Caddyfile" > "$CADDYFILE"
    die "Caddyfile inválido; arquivo original restaurado, Caddy não foi recarregado."
  fi
  docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  echo "Caddy recarregado; o HTTPS de $DOMAIN é emitido automaticamente pelo Caddy."
}

log "Proxy reverso no servidor web do host ($WEB)"
case "$WEB" in
  caddy-docker) setup_caddy_docker; PLUGIN=caddy ;;
  nginx|none) setup_nginx; PLUGIN=nginx ;;
  apache)     setup_apache; PLUGIN=apache ;;
esac

# ---------------------------------------------------------------- HTTPS
if [ "${PLUGIN:-}" = "caddy" ]; then
  :
elif [ "$SKIP_TLS" = "1" ]; then
  warn "SKIP_TLS=1: HTTPS não configurado."
else
  SERVER_IP="$(public_ip)"; RESOLVED="$(dns_ip || true)"
  if [ "$RESOLVED" != "$SERVER_IP" ]; then
    warn "$DOMAIN aponta para '${RESOLVED:-nada}', não para $SERVER_IP. Crie o registro A e rode o script de novo para ativar HTTPS."
  else
    log "Certificado Let's Encrypt ($PLUGIN)"
    dpkg -s certbot >/dev/null 2>&1 || apt-get install -y -qq certbot
    dpkg -s "python3-certbot-$PLUGIN" >/dev/null 2>&1 || apt-get install -y -qq "python3-certbot-$PLUGIN"
    if [ -n "$EMAIL" ]; then MAILOPT=(-m "$EMAIL"); else MAILOPT=(--register-unsafely-without-email); fi
    certbot --"$PLUGIN" -d "$DOMAIN" --non-interactive --agree-tos "${MAILOPT[@]}" --redirect --keep-until-expiring
  fi
fi

log "Pronto"
echo "Local:   http://127.0.0.1:$PORT/  (API em /api/health)"
echo "Público: https://$DOMAIN/  (ou http:// se o HTTPS ainda não foi emitido)"
echo "Backup da config anterior: $BACKUP_DIR"
echo "Desfazer: bash $APP_DIR/deploy/rollback.sh"
