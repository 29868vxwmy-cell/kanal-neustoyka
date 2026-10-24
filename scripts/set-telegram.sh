#!/bin/sh
# Вносит токен бота и адрес канала в .env на сервере. Токен в чат и в git не попадает.
# Запуск на сервере: sh /opt/kanal-neustoyka/scripts/set-telegram.sh
set -eu

DIR=/opt/kanal-neustoyka
ENV="$DIR/.env"

api() {  # api <метод> [<параметры>] — токен идёт через stdin curl, не в списке процессов
	printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TOKEN" "$1" | curl -sS -m 20 -K - ${2:+--data "$2"}
}

printf 'Токен бота из @BotFather (ввод не виден): '
stty -echo; read -r TOKEN; stty echo; echo
printf 'Адрес канала, например @neustoyka_po_delu: '
read -r CHANNEL

case "$CHANNEL" in @*) ;; *) CHANNEL="@$CHANNEL" ;; esac

ME=$(api getMe)
case "$ME" in *'"ok":true'*) ;; *) echo "Токен не подошёл: $ME" | sed "s/$TOKEN/***/g"; exit 1 ;; esac
BOT_ID=$(echo "$ME" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')

MEMBER=$(api getChatMember "chat_id=$CHANNEL&user_id=$BOT_ID")
case "$MEMBER" in
	*'"status":"administrator"'*'"can_post_messages":true'*) echo "Бот — админ канала с правом публикации." ;;
	*) echo "Бот не админ канала $CHANNEL или без права публикации. Ответ Telegram:"; echo "$MEMBER"; exit 1 ;;
esac

MODE=""
if [ -f "$ENV" ]; then
	MODE=$(sed -n 's/^TG_MODE=//p' "$ENV" | tail -1)
	cp -p "$ENV" "$ENV.bak"
fi
[ -n "$MODE" ] || MODE=approve

CODE=$(head -c 9 /dev/urandom | od -An -tx1 | tr -d ' \n')
umask 027
cat > "$ENV" <<EOF
TG_TOKEN=$TOKEN
TG_CHANNEL=$CHANNEL
TG_MODE=$MODE
TG_BIND_CODE=$CODE
EOF
chgrp kanal "$ENV" 2>/dev/null || true

systemctl restart kanal-bot 2>/dev/null || true

echo
echo "Готово. Режим: $MODE (меняется строкой TG_MODE в $ENV или командой /mode в боте)."
echo "Откройте своего бота в Telegram и отправьте ему:"
echo
echo "    /start $CODE"
echo
echo "После этого бот слушается только вас."
