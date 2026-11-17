#!/usr/bin/env bash
# StudyBot: маршрут Obsidian/CouchDB в nginx SNI-роутере 3x-ui-dual-domain.
#   studybot-sni-guard          — добавить маршрут, если его нет (идемпотентно)
#   studybot-sni-guard remove   — убрать маршрут и вхосты StudyBot из nginx
# Запускается systemd-юнитами studybot-sni-guard.{path,timer}: если 3x-ui
# перезапишет /etc/nginx/stream-sni-router.conf, маршрут вернётся сам.
# Любая правка проверяется `nginx -t`; при ошибке конфиг откатывается.
set -euo pipefail

ENV_FILE=/opt/studybot/data/couchdb.env
CONF=/etc/nginx/stream-sni-router.conf
SITES_A=/etc/nginx/sites-available
SITES_E=/etc/nginx/sites-enabled
TAG="# studybot"
log() { echo "$*"; logger -t studybot-sni-guard -- "$*" 2>/dev/null || true; }

[ -f "$CONF" ] || { log "нет $CONF — 3x-ui не установлен, ничего не делаю"; exit 0; }

reload_or_restore() {  # $1 — бэкап конфига
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx
    return 0
  fi
  log "nginx -t не прошёл — откатываю $CONF"
  cp -a "$1" "$CONF"
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
  return 1
}

if [ "${1:-}" = "remove" ]; then
  bak=$(mktemp); cp -a "$CONF" "$bak"
  sed -i "/$TAG\$/d" "$CONF"
  rm -f "$SITES_E/studybot-notes.conf" "$SITES_E/studybot-notes-80.conf"
  reload_or_restore "$bak" && log "маршрут StudyBot удалён из nginx"
  rm -f "$bak"
  exit 0
fi

[ -f "$ENV_FILE" ] || exit 0
# shellcheck disable=SC1090
. "$ENV_FILE"
[ "${EXPOSE:-}" = xui ] || exit 0
: "${NOTES_DOMAIN:?}" "${VHOST_PORT:?}"

changed=0
bak=$(mktemp)
cp -a "$CONF" "$bak"
trap 'rm -f "$bak"' EXIT

# строка в map: <имя> -> наш upstream
if ! grep -q "studybot_notes; *$TAG" "$CONF"; then
  grep -q "map .*ssl_preread_server_name" "$CONF" || { log "в $CONF нет map по SNI — схема не распознана"; exit 1; }
  sed -i "/map .*ssl_preread_server_name/a\\        $NOTES_DOMAIN  studybot_notes;  $TAG" "$CONF"
  changed=1
fi
# upstream на наш вхост (127.0.0.1, принимает proxy protocol)
if ! grep -q "upstream studybot_notes " "$CONF"; then
  anchor=$(grep -m1 -oE "upstream [a-z_]+_backend" "$CONF" || true)
  [ -n "$anchor" ] || { log "в $CONF нет upstream-блоков — схема не распознана"; exit 1; }
  sed -i "0,/$anchor/{/$anchor/a\\    upstream studybot_notes { server 127.0.0.1:$VHOST_PORT; }  $TAG
}" "$CONF"
  changed=1
fi
# вхосты (на случай если ссылки в sites-enabled удалили)
for f in studybot-notes.conf studybot-notes-80.conf; do
  if [ -f "$SITES_A/$f" ] && [ ! -e "$SITES_E/$f" ]; then
    ln -sf "$SITES_A/$f" "$SITES_E/$f"
    changed=1
  fi
done

if [ "$changed" = 1 ]; then
  if reload_or_restore "$bak"; then
    log "маршрут $NOTES_DOMAIN -> 127.0.0.1:$VHOST_PORT добавлен в SNI-роутер"
  else
    exit 1
  fi
fi
