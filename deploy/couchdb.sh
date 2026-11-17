#!/usr/bin/env bash
# Установка CouchDB для Obsidian Self-hosted LiveSync (в Docker) + создание базы
# + запись доступа в конфиг StudyBot. Запуск: sudo bash deploy/couchdb.sh
# Повторный запуск безопасен: существующий контейнер и пароль сохраняются.
set -euo pipefail

APP_DIR=/opt/studybot
DATA_DIR="$APP_DIR/data"
ENV_FILE="$DATA_DIR/couchdb.env"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
DB_NAME="${DB_NAME:-obsidian}"
NET=studybot-net
LOCAL=http://127.0.0.1:5984

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash deploy/couchdb.sh" >&2
  exit 1
fi
mkdir -p "$DATA_DIR"

# ---------- Docker ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "==> Ставлю Docker"
  apt-get update -qq
  apt-get install -y -qq docker.io curl openssl >/dev/null
  systemctl enable --now docker
fi
command -v curl >/dev/null || apt-get install -y -qq curl >/dev/null
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null

# ---------- учётные данные ----------
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  echo "==> Использую сохранённые данные CouchDB из $ENV_FILE"
else
  COUCH_USER=obsidian
  COUCH_PASSWORD="$(openssl rand -hex 16)"
  DOMAIN=""
  echo
  echo "Как Obsidian на ваших устройствах будет подключаться к CouchDB?"
  echo "  1) По домену с HTTPS (рекомендуется; нужен домен, A-запись которого указывает на этот сервер;"
  echo "     откроются порты 80/443 — они не должны быть заняты nginx/apache; сертификат выпустит Caddy)"
  echo "  2) Только локально (127.0.0.1) — доступ настрою сам (свой прокси, Tailscale, SSH-туннель)"
  echo "  3) Открыть порт 5984 без HTTPS (не рекомендуется: пароль идёт открытым текстом, мобильный Obsidian не подключится)"
  read -r -p "Выбор [1/2/3]: " MODE
  case "$MODE" in
    1) read -r -p "Домен (например notes.example.com): " DOMAIN; EXPOSE=domain ;;
    3) EXPOSE=public ;;
    *) EXPOSE=local ;;
  esac
  umask 077
  cat > "$ENV_FILE" <<EOF
COUCH_USER=$COUCH_USER
COUCH_PASSWORD=$COUCH_PASSWORD
EXPOSE=$EXPOSE
DOMAIN=$DOMAIN
EOF
fi

# ---------- контейнер CouchDB ----------
if docker inspect couchdb >/dev/null 2>&1; then
  docker start couchdb >/dev/null
  echo "==> Контейнер couchdb уже есть — запущен"
else
  echo "==> Запускаю CouchDB"
  if [ "$EXPOSE" = public ]; then PUBLISH="-p 5984:5984"; else PUBLISH="-p 127.0.0.1:5984:5984"; fi
  # shellcheck disable=SC2086
  docker run -d --name couchdb --restart unless-stopped --network "$NET" $PUBLISH \
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

# ---------- HTTPS через Caddy ----------
PUBLIC_URL="$LOCAL"
if [ "${EXPOSE:-local}" = domain ] && [ -n "${DOMAIN:-}" ]; then
  if ! docker inspect caddy >/dev/null 2>&1; then
    echo "==> Запускаю Caddy (HTTPS для $DOMAIN)"
    docker run -d --name caddy --restart unless-stopped --network "$NET" -p 80:80 -p 443:443 \
      -v caddy-data:/data caddy:2 caddy reverse-proxy --from "$DOMAIN" --to couchdb:5984 >/dev/null
  else
    docker start caddy >/dev/null
  fi
  PUBLIC_URL="https://$DOMAIN"
elif [ "${EXPOSE:-local}" = public ]; then
  PUBLIC_URL="http://$(hostname -I | awk '{print $1}'):5984"
fi

# ---------- конфиг StudyBot ----------
if [ -x "$APP_DIR/.venv/bin/python" ]; then
  echo "==> Прописываю CouchDB в конфиг StudyBot"
  chown -R "$RUN_USER": "$DATA_DIR"
  sudo -u "$RUN_USER" env COUCH_USER="$COUCH_USER" COUCH_PASSWORD="$COUCH_PASSWORD" DB_NAME="$DB_NAME" \
    "$APP_DIR/.venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, "/opt/studybot")
from studybot import config
config.update(lambda c: c["couchdb"].update({
    "enabled": True,
    "url": "http://127.0.0.1:5984",
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
if [ "${EXPOSE:-local}" = local ]; then
  echo "Доступ только с сервера. Для Obsidian на устройствах настройте прокси/туннель на 127.0.0.1:5984."
fi
