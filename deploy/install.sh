#!/usr/bin/env bash
# Установка/обновление StudyBot на Debian/Ubuntu. Запуск: sudo bash deploy/install.sh
# Повторный запуск обновляет код, не трогая data/ (настройки, сессию, базу).
set -euo pipefail

APP_DIR=/opt/studybot
RUN_USER="${SUDO_USER:-$(id -un)}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash deploy/install.sh" >&2
  exit 1
fi

echo "==> Пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null

echo "==> Код в $APP_DIR (пользователь сервиса: $RUN_USER)"
mkdir -p "$APP_DIR/data"
rm -rf "$APP_DIR/studybot"
cp -r "$SRC_DIR/studybot" "$SRC_DIR/requirements.txt" "$APP_DIR/"
find "$APP_DIR/studybot" -name '__pycache__' -prune -exec rm -rf {} +

echo "==> Виртуальное окружение"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -U pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R "$RUN_USER": "$APP_DIR"
chmod 700 "$APP_DIR/data"

echo "==> Команда studybot"
cat > /usr/local/bin/studybot <<EOF
#!/usr/bin/env bash
# studybot            — панель управления
# studybot setup      — первичная настройка
# studybot login      — перелогиниться в аккаунт
# studybot update     — скачать новую версию с GitHub и переустановить (настройки не трогаются)
SRC_DIR="$SRC_DIR"
if [ "\${1:-}" = "update" ]; then
  if [ ! -d "\$SRC_DIR/.git" ]; then
    echo "\$SRC_DIR — не git-репозиторий. Скачайте заново: git clone https://github.com/puleglo/studybot.git" >&2
    exit 1
  fi
  OWNER="\$(stat -c %U "\$SRC_DIR")"
  echo "==> Скачиваю обновления в \$SRC_DIR"
  OLD="\$(sudo -u "\$OWNER" git -C "\$SRC_DIR" rev-parse --short HEAD)"
  sudo -u "\$OWNER" git -C "\$SRC_DIR" pull --ff-only || { echo "git pull не удался (локальные правки?)" >&2; exit 1; }
  NEW="\$(sudo -u "\$OWNER" git -C "\$SRC_DIR" rev-parse --short HEAD)"
  if [ "\$OLD" = "\$NEW" ] && [ "\${2:-}" != "--force" ]; then
    echo "Уже последняя версия (\$NEW). Переустановить всё равно: studybot update --force"
    exit 0
  fi
  sudo -u "\$OWNER" git -C "\$SRC_DIR" log --oneline "\$OLD..\$NEW" 2>/dev/null | sed 's/^/    /'
  exec sudo STUDYBOT_UPDATE=1 bash "\$SRC_DIR/deploy/install.sh"
fi
if [ "\$(id -un)" != "$RUN_USER" ]; then exec sudo -u "$RUN_USER" "\$0" "\$@"; fi
cd "$APP_DIR"
[ \$# -eq 0 ] && set -- panel
exec "$APP_DIR/.venv/bin/python" -m studybot "\$@"
EOF
chmod 755 /usr/local/bin/studybot

echo "==> systemd"
sed "s/__USER__/$RUN_USER/" "$SRC_DIR/deploy/studybot.service" > /etc/systemd/system/studybot.service
systemctl daemon-reload
if systemctl is-active --quiet studybot; then
  systemctl restart studybot
  echo "Сервис перезапущен с новым кодом."
fi

echo "==> CouchDB для Obsidian"
if [ -f "$APP_DIR/data/couchdb.env" ]; then
  echo "Уже установлена этим скриптом (данные: $APP_DIR/data/couchdb.env)."
elif curl -fs -o /dev/null http://127.0.0.1:5984/ 2>/dev/null; then
  echo "На сервере уже есть CouchDB — укажите её в 'studybot setup', базу бот создаст сам."
elif [ -n "${STUDYBOT_UPDATE:-}" ]; then
  echo "Не установлена (поставить: sudo bash $SRC_DIR/deploy/couchdb.sh)."
else
  read -r -p "CouchDB не найдена. Установить и создать базу для Obsidian? [Y/n]: " ans
  if [ "${ans:-Y}" != "n" ] && [ "${ans:-Y}" != "N" ]; then
    RUN_USER="$RUN_USER" bash "$SRC_DIR/deploy/couchdb.sh"
  fi
fi

cat <<EOF

Готово.
  Обновление:    studybot update
  1) Настройка:  studybot setup
  2) Запуск:     sudo systemctl enable --now studybot
  3) Панель:     studybot          (с компьютера: ssh <vps> -t studybot)
  Логи:          journalctl -u studybot -f
EOF
