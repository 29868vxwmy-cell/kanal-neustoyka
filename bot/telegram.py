"""Минимальный клиент Telegram Bot API на стандартной библиотеке."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, timeout: float = 70):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    def call(self, method: str, **params):
        data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(
            self.base + method, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read() or b"{}")
        if not body.get("ok"):
            # Токен в текст ошибки не попадает: в base он есть, в описании ответа нет.
            raise TelegramError(f"{method}: {body.get('description', 'нет ответа')}")
        return body["result"]

    def send(self, chat_id, text: str, buttons: list[list[tuple[str, str]]] | None = None,
             reply_to: int | None = None):
        markup = None
        if buttons:
            markup = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in buttons
                ]
            }
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_markup=markup,
            reply_parameters={"message_id": reply_to} if reply_to else None,
        )

    def updates(self, offset: int | None, timeout: int = 50):
        return self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    def answer(self, callback_id: str, text: str = ""):
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def drop_buttons(self, chat_id, message_id: int):
        return self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                         reply_markup={"inline_keyboard": []})

    def delete(self, chat_id, message_id: int):
        return self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
