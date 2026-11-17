#!/usr/bin/env bash
# Установка CouchDB для Obsidian Self-hosted LiveSync (в Docker) + создание базы
# + запись доступа в конфиг StudyBot. Запуск: sudo bash deploy/couchdb.sh
# Повторный запуск безопасен: существующий контейнер, порты и пароль сохраняются.
# Если стандартные порты (5984, 80, 443) уже кем-то заняты — выбираются свободные.
set -euo pipefail

APP_DIR=/opt/studybot
DATA_DIR="$APP_DIR/data"
ENV_FILE="$DATA_DIR/couchdb.env"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
DB_NAME="${DB_NAME:-obsidian}"
NET=studybot-net

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash deploy/couchdb.sh" >&2
  exit 1
fi
mkdir -p "$DATA_DIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 3x-ui-dual-domain: nginx stream SNI-роутер на 443
XUI_CONF=/etc/nginx/stream-sni-router.conf
XUI=0
if [ -f "$XUI_CONF" ] && grep -q "ssl_preread_server_name" "$XUI_CONF"; then XUI=1; fi

# ---------- порты ----------
port_busy() {  # 0 — порт кто-то слушает (TCP, любой адрес)
  if command -v ss >/dev/null 2>&1; then
    ss -Hltn "( sport = :$1 )" 2>/dev/null | grep -q .
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
  fi
}
who_listens() {
  command -v ss >/dev/null 2>&1 && ss -Hltnp "( sport = :$1 )" 2>/dev/null \
    | grep -o 'users:(("[^"]*' | head -1 | cut -d'"' -f2 || true
}
who_txt() {  # " (nginx)" или пусто
  local w; w=$(who_listens "$1"); [ -n "$w" ] && echo " ($w)" || true
}
free_port() {  # free_port <первый> <последний>
  local p
  for p in $(seq "$1" "$2"); do
    port_busy "$p" || { echo "$p"; return 0; }
  done
  return 1
}
pick_port() {  # pick_port <желаемый> <запасной-от> <запасной-до> <описание>
  local want=$1 alt
  if ! port_busy "$want"; then echo "$want"; return 0; fi
  alt=$(free_port "$2" "$3") || { echo "Нет свободных портов $2–$3 для $4" >&2; return 1; }
  echo "    порт $want занят$(who_txt $want), $4 будет на $alt" >&2
  echo "$alt"
}

# ---------- Docker ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "==> Ставлю Docker"
  apt-get update -qq
  apt-get install -y -qq docker.io curl openssl >/dev/null
  systemctl enable --now docker
fi
command -v curl >/dev/null || apt-get install -y -qq curl >/dev/null
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null

# ---------- учётные данные и порты (выбираются один раз) ----------
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  echo "==> Использую сохранённые данные CouchDB из $ENV_FILE"
else
  COUCH_USER=obsidian
  COUCH_PASSWORD="$(openssl rand -hex 16)"
  DOMAIN=""; HTTP_PORT=""; HTTPS_PORT=""; NOTES_DOMAIN=""; VHOST_PORT=""
  echo
  if [ "$XUI" = 1 ]; then
  echo "Обнаружен 3x-ui-dual-domain: nginx на 443 маршрутизирует по SNI, сертификаты выпускает certbot."
  echo "Как Obsidian на ваших устройствах будет подключаться к CouchDB?"
  echo "  1) Через nginx 3x-ui по отдельному имени (рекомендуется): HTTPS на 443, новых открытых портов нет."
  echo "     Нужно третье имя (например notes.example.com) с A-записью на этот сервер."
  echo "  2) Только локально (127.0.0.1) — доступ настрою сам"
  read -r -p "Выбор [1/2]: " MODE
  if [ "${MODE:-1}" = 1 ]; then
    read -r -p "Имя для Obsidian (например notes.example.com): " NOTES_DOMAIN
    NOTES_DOMAIN="$(echo "$NOTES_DOMAIN" | tr 'A-Z' 'a-z' | tr -d '[:space:]')"
    if ! [[ "$NOTES_DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
      echo "Некорректное имя: $NOTES_DOMAIN" >&2; exit 1
    fi
    if grep -qE "^[[:space:]]*${NOTES_DOMAIN//./\\.}[[:space:]]" "$XUI_CONF"; then
      echo "Имя $NOTES_DOMAIN уже занято в SNI-роутере 3x-ui (это панельный или основной домен). Нужно другое." >&2; exit 1
    fi
    EXPOSE=xui
  else
    EXPOSE=local
  fi
  else
  echo "Как Obsidian на ваших устройствах будет подключаться к CouchDB?"
  echo "  1) По домену с HTTPS (рекомендуется; нужен домен, A-запись которого указывает на этот сервер;"
  echo "     сертификат выпустит Caddy автоматически)"
  echo "  2) Только локально (127.0.0.1) — доступ настрою сам (свой прокси, Tailscale, SSH-туннель)"
  echo "  3) Открыть порт CouchDB без HTTPS (не рекомендуется: пароль идёт открытым текстом, мобильный Obsidian не подключится)"
  read -r -p "Выбор [1/2/3]: " MODE
  case "$MODE" in
    1) read -r -p "Домен (например notes.example.com): " DOMAIN; EXPOSE=domain ;;
    3) EXPOSE=public ;;
    *) EXPOSE=local ;;
  esac
  fi

  echo "==> Проверяю порты"
  COUCH_PORT=$(pick_port 5984 5985 5999 "CouchDB")
  if [ "$EXPOSE" = xui ]; then
    # loopback-порт для нашего nginx-вхоста (3x-ui использует 8080–8447)
    VHOST_PORT=$(free_port 8490 8530) || { echo "Нет свободного порта 8490–8530 для вхоста" >&2; exit 1; }
  fi

  if [ "$EXPOSE" = domain ]; then
    b80=0; b443=0
    port_busy 80 && b80=1
    port_busy 443 && b443=1
    if [ $b80 = 0 ] && [ $b443 = 0 ]; then
      HTTP_PORT=80; HTTPS_PORT=443
    elif [ $b443 = 1 ] && [ $b80 = 0 ]; then
      # сертификат получим через HTTP-проверку на 80, сам HTTPS — на другом порту
      HTTP_PORT=80
      HTTPS_PORT=$(free_port 8443 8499)
      echo "    порт 443 занят$(who_txt 443), HTTPS будет на $HTTPS_PORT"
    elif [ $b80 = 1 ] && [ $b443 = 0 ]; then
      # сертификат получим через TLS-проверку на 443
      HTTP_PORT=none
      HTTPS_PORT=443
      echo "    порт 80 занят$(who_txt 80) — не страшно, сертификат выпустится через 443"
    else
      echo "    порты 80 и 443 заняты$(who_txt 443) — уже стоит веб-сервер."
      echo "    Caddy не ставлю: CouchDB будет слушать 127.0.0.1:$COUCH_PORT, в конце покажу конфиг для вашего nginx."
      EXPOSE=proxy
    fi
  fi

  umask 077
  cat > "$ENV_FILE" <<EOF
COUCH_USER=$COUCH_USER
COUCH_PASSWORD=$COUCH_PASSWORD
EXPOSE=$EXPOSE
DOMAIN=$DOMAIN
COUCH_PORT=$COUCH_PORT
HTTP_PORT=$HTTP_PORT
HTTPS_PORT=$HTTPS_PORT
NOTES_DOMAIN=$NOTES_DOMAIN
VHOST_PORT=$VHOST_PORT
EOF
fi
COUCH_PORT="${COUCH_PORT:-5984}"   # env-файлы от прошлых версий
HTTP_PORT="${HTTP_PORT:-80}"
HTTPS_PORT="${HTTPS_PORT:-443}"
LOCAL="http://127.0.0.1:$COUCH_PORT"

# ---------- контейнер CouchDB ----------
if docker inspect couchdb >/dev/null 2>&1; then
  docker start couchdb >/dev/null
  echo "==> Контейнер couchdb уже есть — запущен"
else
  if port_busy "$COUCH_PORT"; then  # заняли, пока выбирали / между запусками
    COUCH_PORT=$(pick_port "$COUCH_PORT" 5985 5999 "CouchDB")
    LOCAL="http://127.0.0.1:$COUCH_PORT"
    sed -i "s/^COUCH_PORT=.*/COUCH_PORT=$COUCH_PORT/" "$ENV_FILE"
  fi
  echo "==> Запускаю CouchDB на порту $COUCH_PORT"
  if [ "$EXPOSE" = public ]; then BIND="0.0.0.0"; else BIND="127.0.0.1"; fi
  docker run -d --name couchdb --restart unless-stopped --network "$NET" \
    -p "$BIND:$COUCH_PORT:5984" \
    -e COUCHDB_USER="$COUCH_USER" -e COUCHDB_PASSWORD="$COUCH_PASSWORD" \
    -v couchdb-data:/opt/couchdb/data -v couchdb-config:/opt/couchdb/etc/local.d \
    couchdb:3 >/dev/null
fi

echo -n "==> Жду CouchDB"
for _ in $(seq 1 60); do
  curl -fs "$LOCAL/_up" >/dev/null 2>&1 && break
  echo -n "."; sleep 1
done
echo
curl -fs "$LOCAL/_up" >/dev/null || { echo "CouchDB не запустилась: docker logs couchdb" >&2; exit 1; }

AUTH="$COUCH_USER:$COUCH_PASSWORD"
cfg() {  # cfg <секция> <ключ> <значение-JSON>
  curl -fs -X PUT "$LOCAL/_node/_local/_config/$1/$2" -u "$AUTH" -H 'Content-Type: application/json' -d "$3" >/dev/null
}

echo "==> Настраиваю CouchDB под LiveSync"
curl -fs -X POST "$LOCAL/_cluster_setup" -u "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"action\":\"enable_single_node\",\"username\":\"$COUCH_USER\",\"password\":\"$COUCH_PASSWORD\",\"bind_address\":\"0.0.0.0\",\"port\":5984,\"singlenode\":true}" \
  >/dev/null 2>&1 || true   # при повторном запуске уже настроено
cfg chttpd require_valid_user '"true"'
cfg chttpd_auth require_valid_user '"true"'
cfg httpd WWW-Authenticate '"Basic realm=\"couchdb\""'
cfg httpd enable_cors '"true"'
cfg chttpd enable_cors '"true"'
cfg chttpd max_http_request_size '"4294967296"'
cfg couchdb max_document_size '"50000000"'
cfg cors credentials '"true"'
cfg cors origins '"app://obsidian.md,capacitor://localhost,http://localhost"'

echo "==> База «$DB_NAME»"
code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$LOCAL/$DB_NAME" -u "$AUTH")
case "$code" in
  201|202) echo "    создана" ;;
  412)     echo "    уже существует" ;;
  *)       echo "    ошибка создания базы: HTTP $code" >&2; exit 1 ;;
esac

# ---------- HTTPS через nginx 3x-ui (SNI на 443) ----------
xui_cleanup() {
  rm -f /etc/nginx/sites-enabled/studybot-notes.conf /etc/nginx/sites-available/studybot-notes.conf
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
}
xui_setup() {
  local A=/etc/nginx/sites-available E=/etc/nginx/sites-enabled webroot server_ip resolved
  command -v nginx >/dev/null && command -v certbot >/dev/null || { echo "Нет nginx/certbot — это точно 3x-ui?" >&2; return 1; }
  nginx -t >/dev/null 2>&1 || { echo "nginx -t уже сейчас с ошибкой — ничего не трогаю, сначала почините nginx" >&2; return 1; }
  webroot=$(grep -hoP 'acme-challenge/\s*\{\s*root\s+\K[^;]+' $A/80-redirect.conf 2>/dev/null | head -1 || true)
  webroot="${webroot:-/var/www/html}"
  server_ip=$(grep -oP '^\s*listen\s+\K[0-9.]+(?=:443;)' "$XUI_CONF" | head -1 || true)

  echo "==> DNS $NOTES_DOMAIN"
  resolved=$(getent ahostsv4 "$NOTES_DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)
  if [ -z "$resolved" ]; then
    echo "    $NOTES_DOMAIN не резолвится. Создайте A-запись на ${server_ip:-IP сервера} и запустите скрипт снова." >&2; return 1
  elif [ -n "$server_ip" ] && [ "$resolved" != "$server_ip" ]; then
    echo "    $NOTES_DOMAIN -> $resolved, а сервер слушает $server_ip (CDN/прокси в DNS надо выключить)."
    read -r -p "    Продолжить всё равно? [y/N]: " a; [ "$a" = y ] || [ "$a" = Y ] || return 1
  fi

  echo "==> nginx: порт 80 для $NOTES_DOMAIN (проверка Let's Encrypt)"
  mkdir -p "$webroot/.well-known/acme-challenge"
  cat > "$A/studybot-notes-80.conf" <<EOF
# StudyBot: выпуск/продление сертификата для Obsidian (CouchDB)
server {
    listen 80;
    server_name $NOTES_DOMAIN;
    server_tokens off;
    location /.well-known/acme-challenge/ { root $webroot; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF
  ln -sf "$A/studybot-notes-80.conf" "$E/studybot-notes-80.conf"
  if ! nginx -t >/dev/null 2>&1; then
    rm -f "$E/studybot-notes-80.conf" "$A/studybot-notes-80.conf"
    echo "nginx не принял конфиг порта 80 — откатил" >&2; return 1
  fi
  systemctl reload nginx

  echo "==> Сертификат для $NOTES_DOMAIN (certbot webroot, продлевается cron'ом 3x-ui)"
  certbot certonly --webroot -w "$webroot" -d "$NOTES_DOMAIN" --non-interactive --agree-tos \
    --keep-until-expiring --register-unsafely-without-email >/dev/null \
    || { echo "certbot не выпустил сертификат (см. /var/log/letsencrypt/letsencrypt.log)" >&2; return 1; }

  echo "==> nginx: вхост 127.0.0.1:$VHOST_PORT -> CouchDB 127.0.0.1:$COUCH_PORT"
  cat > "$A/studybot-notes.conf" <<EOF
# StudyBot: Obsidian LiveSync -> CouchDB. Трафик приходит из SNI-роутера 3x-ui (proxy protocol).
server {
    listen 127.0.0.1:$VHOST_PORT ssl proxy_protocol;
    server_name $NOTES_DOMAIN;
    server_tokens off;
    ssl_certificate     /etc/letsencrypt/live/$NOTES_DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$NOTES_DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 0;

    location / {
        proxy_pass http://127.0.0.1:$COUCH_PORT;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_protocol_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;   # LiveSync держит долгие запросы _changes
        proxy_send_timeout 3600s;
    }
}
EOF
  ln -sf "$A/studybot-notes.conf" "$E/studybot-notes.conf"
  if ! nginx -t >/dev/null 2>&1; then
    nginx -t || true
    xui_cleanup; echo "nginx не принял вхост — откатил" >&2; return 1
  fi
  systemctl reload nginx

  echo "==> Маршрут в SNI-роутере 3x-ui + сторож"
  install -m 755 "$SCRIPT_DIR/sni-guard.sh" /usr/local/sbin/studybot-sni-guard
  cat > /etc/systemd/system/studybot-sni-guard.service <<'EOF'
[Unit]
Description=StudyBot: keep Obsidian route in 3x-ui nginx SNI router
After=nginx.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/studybot-sni-guard
EOF
  cat > /etc/systemd/system/studybot-sni-guard.path <<EOF
[Unit]
Description=StudyBot: watch 3x-ui SNI router config

[Path]
PathChanged=$XUI_CONF

[Install]
WantedBy=multi-user.target
EOF
  cat > /etc/systemd/system/studybot-sni-guard.timer <<'EOF'
[Unit]
Description=StudyBot: periodic check of Obsidian route

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  if ! /usr/local/sbin/studybot-sni-guard; then
    xui_cleanup; echo "Не удалось добавить маршрут в SNI-роутер — откатил" >&2; return 1
  fi
  systemctl enable --now studybot-sni-guard.path studybot-sni-guard.timer >/dev/null 2>&1

  echo -n "==> Проверка https://$NOTES_DOMAIN через 443: "
  if curl -fs --max-time 15 ${server_ip:+--resolve "$NOTES_DOMAIN:443:$server_ip"} \
       -u "$COUCH_USER:$COUCH_PASSWORD" "https://$NOTES_DOMAIN/_up" >/dev/null; then
    echo "OK"
  else
    echo "не ответил (проверьте DNS и 'sudo studybot-sni-guard'; healthcheck 3x-ui)"
  fi
}

# ---------- HTTPS через Caddy ----------
OPEN_PORTS=""
PUBLIC_URL="$LOCAL"
if [ "$EXPOSE" = xui ]; then
  if ! xui_setup; then
    echo "Режим 3x-ui не настроен. CouchDB работает локально на $LOCAL — бот будет писать в неё как обычно." >&2
  else
    PUBLIC_URL="https://$NOTES_DOMAIN"
  fi
fi
if [ "$EXPOSE" = domain ] && [ -n "${DOMAIN:-}" ]; then
  if ! docker inspect caddy >/dev/null 2>&1; then
    echo "==> Запускаю Caddy (HTTPS для $DOMAIN на порту $HTTPS_PORT)"
    PUBLISH=(-p "$HTTPS_PORT:$HTTPS_PORT")
    if [ "$HTTP_PORT" = 80 ] && ! port_busy 80; then PUBLISH+=(-p 80:80); fi
    if [ "$HTTPS_PORT" = 443 ]; then FROM="$DOMAIN"; else FROM="https://$DOMAIN:$HTTPS_PORT"; fi
    docker run -d --name caddy --restart unless-stopped --network "$NET" "${PUBLISH[@]}" \
      -v caddy-data:/data caddy:2 caddy reverse-proxy --from "$FROM" --to couchdb:5984 >/dev/null
  else
    docker start caddy >/dev/null
  fi
  if [ "$HTTPS_PORT" = 443 ]; then PUBLIC_URL="https://$DOMAIN"; else PUBLIC_URL="https://$DOMAIN:$HTTPS_PORT"; fi
  OPEN_PORTS="$HTTPS_PORT"
  [ "$HTTP_PORT" = 80 ] && OPEN_PORTS="$OPEN_PORTS 80"
elif [ "$EXPOSE" = public ]; then
  PUBLIC_URL="http://$(hostname -I | awk '{print $1}'):$COUCH_PORT"
  OPEN_PORTS="$COUCH_PORT"
fi

# открыть порты в UFW, если он включён
if [ -n "$OPEN_PORTS" ] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  for p in $OPEN_PORTS; do ufw allow "$p/tcp" >/dev/null && echo "==> UFW: открыт порт $p"; done
fi

# ---------- конфиг StudyBot ----------
if [ -x "$APP_DIR/.venv/bin/python" ]; then
  echo "==> Прописываю CouchDB в конфиг StudyBot"
  chown -R "$RUN_USER": "$DATA_DIR"
  sudo -u "$RUN_USER" env COUCH_USER="$COUCH_USER" COUCH_PASSWORD="$COUCH_PASSWORD" DB_NAME="$DB_NAME" \
    COUCH_URL="$LOCAL" "$APP_DIR/.venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, "/opt/studybot")
from studybot import config
config.update(lambda c: c["couchdb"].update({
    "enabled": True,
    "url": os.environ["COUCH_URL"],
    "database": os.environ["DB_NAME"],
    "username": os.environ["COUCH_USER"],
    "password": os.environ["COUCH_PASSWORD"],
}))
PY
fi

cat <<EOF

CouchDB готова. В Obsidian: Self-hosted LiveSync → Remote Configuration → CouchDB:
  URI:       $PUBLIC_URL
  Username:  $COUCH_USER
  Password:  $COUCH_PASSWORD
  Database:  $DB_NAME
  End-to-End Encryption: ВЫКЛ, Path Obfuscation: ВЫКЛ (иначе бот не сможет писать файлы)
Данные сохранены в $ENV_FILE (показать ещё раз: sudo cat $ENV_FILE)
EOF
case "$EXPOSE" in
  xui)
    echo "Маршрут живёт в nginx 3x-ui; если 3x-ui перезапишет конфиг роутера, сторож (studybot-sni-guard) вернёт его сам."
    echo "Убрать маршрут: sudo studybot-sni-guard remove" ;;
  local)
    echo "Доступ только с сервера. Для Obsidian на устройствах настройте прокси/туннель на 127.0.0.1:$COUCH_PORT." ;;
  proxy)
    cat <<EOF

Порты 80/443 заняты вашим веб-сервером. Добавьте в его конфиг (пример для nginx, в server { } вашего домена):

    location / {
        proxy_pass http://127.0.0.1:$COUCH_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        client_max_body_size 0;
    }

и укажите в Obsidian URI https://<ваш-домен>.
EOF
    ;;
esac
