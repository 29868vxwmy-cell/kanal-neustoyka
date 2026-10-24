# Канал про неустойку

Автоматическое ведение Telegram-канала калькулятора https://kalkulyator-neustoyki.ru.

Как это работает:

1. Каждый день задача Claude по расписанию пишет черновики по [prompts/routine.md](prompts/routine.md)
   и кладёт их в `posts/queue/` ветки `main`.
2. Сервер каждые 10 минут забирает их из GitHub (`kanal-pull.timer`).
3. Бот (`kanal-bot.service`) в дни постов (пн, ср, пт, 10:00 МСК) берёт следующий черновик и проверяет его:
   есть ли ссылка на источник и на калькулятор, нет ли посторонних ссылок и обещаний вроде «гарантированно»,
   совпадает ли ключевая ставка с сайтом ЦБ, влезает ли пост в лимит Telegram.
4. Дальше зависит от режима:
   - `approve` — черновик приходит владельцу в личку бота с кнопками «Опубликовать», «На завтра», «В корзину».
     Чтобы поправить текст, ответьте на черновик своим вариантом: бот пришлёт его снова с кнопками.
   - `auto` — бот публикует сам и присылает владельцу копию с кнопкой «Удалить из канала».
     Черновик, не прошедший проверку, всё равно уходит на одобрение.
5. Срочные посты (`urgent: true`, например решение ЦБ) идут вне расписания, с 9 до 21 МСК.

Режим: строка `TG_MODE=approve|auto` в `/opt/kanal-neustoyka/.env` (после правки `systemctl restart kanal-bot`)
или команда боту `/mode approve` / `/mode auto` (действует, пока не сменят снова). `/status` — что в очереди.

## Файлы

- `bot/` — бот на стандартной библиотеке Python 3.12, без зависимостей.
- `posts/queue/` — черновики, формат описан в [prompts/routine.md](prompts/routine.md).
- `content/topics.md` — банк тем.
- `deploy/` — юниты systemd, `scripts/` — установка, внесение токена, обновление.

Проверить черновик вручную: `python3 -m bot.kanal check --rate 14 posts/queue/<файл>.md`.
Тесты: `python3 -m unittest`.

## Установка на сервер (один раз)

Выполняется с согласия владельца.

1. Владелец создаёт канал и бота в @BotFather и делает бота администратором канала
   с правами «Публикация сообщений» и «Удаление сообщений».
2. На сервере создаётся deploy-ключ только для чтения, владелец добавляет его в Settings → Deploy keys репозитория:
   ```
   ssh-keygen -t ed25519 -N '' -f /root/.ssh/kanal_deploy -C kanal-neustoyka
   cat >> /root/.ssh/config <<'EOF'
   Host github-kanal
     HostName github.com
     IdentityFile /root/.ssh/kanal_deploy
     IdentitiesOnly yes
   EOF
   git clone github-kanal:29868vxwmy-cell/kanal-neustoyka.git /opt/kanal-neustoyka
   ```
3. `sh /opt/kanal-neustoyka/scripts/install.sh` — пользователь `kanal`, юниты, таймер, тесты.
4. `sh /opt/kanal-neustoyka/scripts/set-telegram.sh` — спросит токен (ввод не виден) и адрес канала,
   проверит права бота и выдаст код. Отправьте боту `/start <код>` — после этого он слушается только вас.
5. Перед первым постом стартовым черновикам 01–10 проставляется `publish_on` по дням постов.

Логи: `journalctl -u kanal-bot -f`. Состояние (что опубликовано): `/var/lib/kanal-neustoyka/state.json`.
