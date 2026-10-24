#!/bin/sh
# Первая установка на сервере (запускает root). Повторный запуск безопасен.
# Перед этим репозиторий склонирован в /opt/kanal-neustoyka по deploy-ключу (см. README).
set -eu

DIR=/opt/kanal-neustoyka
cd "$DIR"

id kanal >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin kanal
install -d -o kanal -g kanal -m 750 /var/lib/kanal-neustoyka

install -m 644 deploy/kanal-bot.service /etc/systemd/system/
install -m 644 deploy/kanal-pull.service /etc/systemd/system/
install -m 644 deploy/kanal-pull.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kanal-pull.timer

python3 -m unittest -q

if [ -f "$DIR/.env" ]; then
	systemctl enable --now kanal-bot
	systemctl restart kanal-bot
	echo "Бот запущен: systemctl status kanal-bot"
else
	echo "Осталось внести токен: sh $DIR/scripts/set-telegram.sh, затем systemctl enable --now kanal-bot"
fi
