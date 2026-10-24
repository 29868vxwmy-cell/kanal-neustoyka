"""Бот канала: берёт черновики из posts/queue, шлёт владельцу на одобрение или публикует сам.

Запуск на сервере: python3 -m bot.kanal run (из корня репозитория, .env подключает systemd).
Проверка без Telegram: python3 -m bot.kanal check [--rate 14] posts/queue/<файл>.md ...
(--rate — ставка ЦБ вручную, если сайт ЦБ недоступен с этой машины)
"""

from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .posts import Post, check_post, fetch_key_rate, load_queue, parse_post
from .telegram import Telegram, TelegramError

log = logging.getLogger("kanal")
MSK = ZoneInfo("Europe/Moscow")
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

DONE = ("published", "rejected")


@dataclass
class Config:
    token: str
    channel: str
    mode: str = "approve"
    bind_code: str = ""
    repo_dir: Path = Path(".")
    state_file: Path = Path("state.json")
    slot_days: tuple[int, ...] = (0, 2, 4)
    slot_hour: int = 10
    git_pull: bool = False
    pull_every_min: int = 10

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        mode = env.get("TG_MODE", "approve").strip()
        if mode not in ("approve", "auto"):
            raise SystemExit("TG_MODE должен быть approve или auto")
        return cls(
            token=env["TG_TOKEN"],
            channel=env["TG_CHANNEL"],
            mode=mode,
            bind_code=env.get("TG_BIND_CODE", ""),
            repo_dir=Path(env.get("KANAL_REPO_DIR", ".")),
            state_file=Path(env.get("KANAL_STATE", "state.json")),
            slot_days=tuple(int(d) for d in env.get("KANAL_SLOT_DAYS", "0,2,4").split(",")),
            slot_hour=int(env.get("KANAL_SLOT_HOUR", "10")),
            git_pull=env.get("KANAL_GIT_PULL", "1") == "1",
        )


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"owner_id": None, "mode": None, "posts": {}, "last_slot": None,
                     "offset": None}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def post(self, post_id: str) -> dict:
        return self.data["posts"].setdefault(post_id, {})


TAGS = {"bold": "b", "italic": "i", "underline": "u", "strikethrough": "s", "code": "code",
        "pre": "pre", "blockquote": "blockquote"}


def entities_to_html(text: str, entities: list[dict]) -> str:
    """Текст сообщения Telegram с разметкой (жирный, ссылки) обратно в HTML для публикации."""
    # Смещения в Bot API считаются в единицах UTF-16: эмодзи занимает две.
    chars, pos = [], 0
    for ch in text:
        chars.append((pos, ch))
        pos += 2 if ord(ch) > 0xFFFF else 1

    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}
    for e in sorted(entities, key=lambda e: (e["offset"], -e["length"])):
        start, end = e["offset"], e["offset"] + e["length"]
        if e["type"] == "text_link":
            tag_open, tag_close = f'<a href="{html.escape(e["url"])}">', "</a>"
        elif e["type"] in TAGS:
            tag_open, tag_close = f"<{TAGS[e['type']]}>", f"</{TAGS[e['type']]}>"
        else:
            continue  # обычные url Telegram покажет ссылкой и так
        opens.setdefault(start, []).append(tag_open)
        closes.setdefault(end, []).insert(0, tag_close)

    out = []
    for i, ch in chars:
        out.extend(closes.get(i, []))
        out.extend(opens.get(i, []))
        out.append(html.escape(ch, quote=False))
    out.extend(closes.get(pos, []))
    return "".join(out)


class Kanal:
    def __init__(self, cfg: Config, api: Telegram, state: State, key_rate=fetch_key_rate,
                 now=lambda: datetime.now(MSK)):
        self.cfg = cfg
        self.api = api
        self.state = state
        self.key_rate = key_rate
        self.now = now
        self.last_pull = 0.0

    # ---------- режим и очередь ----------

    @property
    def mode(self) -> str:
        return self.state.data.get("mode") or self.cfg.mode

    @property
    def owner(self):
        return self.state.data.get("owner_id")

    def queue(self) -> list[Post]:
        posts, errors = load_queue(self.cfg.repo_dir / "posts" / "queue")
        warned = self.state.data.setdefault("warned", [])
        for err in errors:
            if err not in warned and self.owner:
                warned.append(err)
                self.tell_owner(f"Не читается черновик: {html.escape(err)}")
        return posts

    def pending(self, today: date) -> list[Post]:
        result = []
        for post in self.queue():
            st = self.state.data["posts"].get(post.id, {})
            if st.get("status") in DONE or st.get("status") == "sent":
                continue
            if st.get("postpone_until") and date.fromisoformat(st["postpone_until"]) > today:
                continue
            if post.publish_on and post.publish_on > today:
                continue
            result.append(post)
        return result

    def text_of(self, post: Post) -> str:
        return self.state.data["posts"].get(post.id, {}).get("text_override") or post.text

    def problems(self, post: Post) -> list[str]:
        edited = Post(post.id, self.text_of(post), post.path, post.publish_on, post.urgent,
                      post.topic, post.sources)
        return check_post(edited, self.key_rate())

    # ---------- такт ----------

    def tick(self):
        now = self.now()
        today = now.date()
        self.pull()
        if not self.owner:
            return  # пока владелец не привязан, ничего не шлём и не публикуем

        for post in self.pending(today):
            if post.urgent and 9 <= now.hour < 21:
                self.handle(post, "срочная новость")

        if (today.weekday() in self.cfg.slot_days and now.hour >= self.cfg.slot_hour
                and self.state.data.get("last_slot") != today.isoformat()):
            self.state.data["last_slot"] = today.isoformat()
            regular = [p for p in self.pending(today) if not p.urgent]
            if regular:
                self.handle(regular[0], "пост по расписанию")
            else:
                self.tell_owner("Сегодня день поста, но в очереди нет готового черновика. "
                                "Проверьте задачу Claude, которая пишет посты.")
            self.state.save()

    def handle(self, post: Post, why: str):
        problems = self.problems(post)
        if self.mode == "auto" and not problems:
            self.publish(post)
        else:
            self.send_for_approval(post, problems, why)

    def send_for_approval(self, post: Post, problems: list[str], why: str):
        head = f"<b>Черновик на одобрение</b> ({why})\n<code>{html.escape(post.id)}</code>"
        if problems:
            head += "\n\n⚠️ Автопроверка не пройдена:\n" + "\n".join(
                f"• {html.escape(p)}" for p in problems)
        if post.sources:
            head += "\n\nИсточники:\n" + "\n".join(html.escape(s) for s in post.sources)
        self.api.send(self.owner, head)
        msg = self.api.send(self.owner, self.text_of(post), buttons=[
            [("✅ Опубликовать", f"pub:{post.id}")],
            [("⏭ На завтра", f"later:{post.id}"), ("🗑 В корзину", f"drop:{post.id}")],
        ])
        st = self.state.post(post.id)
        st.update(status="sent", draft_msg_id=msg["message_id"])
        self.state.save()

    def publish(self, post: Post):
        msg = self.api.send(self.cfg.channel, self.text_of(post))
        st = self.state.post(post.id)
        st.update(status="published", channel_msg_id=msg["message_id"],
                  at=self.now().isoformat(timespec="minutes"))
        self.state.save()
        log.info("опубликован %s", post.id)
        note = "Опубликовано в канале" if self.mode == "approve" else "Опубликовано автоматически"
        self.api.send(self.owner, f"{note}: <code>{html.escape(post.id)}</code>",
                      buttons=[[("❌ Удалить из канала", f"del:{post.id}")]])

    def tell_owner(self, text: str):
        if self.owner:
            self.api.send(self.owner, text)

    def pull(self):
        if not self.cfg.git_pull or time.monotonic() - self.last_pull < self.cfg.pull_every_min * 60:
            return
        self.last_pull = time.monotonic()
        res = subprocess.run(["git", "-C", str(self.cfg.repo_dir), "pull", "--ff-only", "-q"],
                             capture_output=True, text=True)
        if res.returncode:
            log.warning("git pull: %s", res.stderr.strip())

    # ---------- входящие ----------

    def on_update(self, upd: dict):
        if "callback_query" in upd:
            self.on_button(upd["callback_query"])
        elif "message" in upd:
            self.on_message(upd["message"])

    def on_message(self, msg: dict):
        user = msg.get("from", {}).get("id")
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            return
        text = (msg.get("text") or "").strip()

        if text.startswith("/start"):
            code = text[len("/start"):].strip()
            if not self.owner and self.cfg.bind_code and code == self.cfg.bind_code:
                self.state.data["owner_id"] = user
                self.state.save()
                self.api.send(user, f"Готово, вы владелец бота. Режим: <b>{self.mode}</b>.\n"
                                    "Команды: /status, /mode approve, /mode auto")
            elif user == self.owner:
                self.api.send(user, "Вы уже владелец. /status — что в очереди.")
            return

        if user != self.owner:
            return  # чужим бот не отвечает

        if text == "/status":
            self.api.send(user, self.status())
        elif text.startswith("/mode"):
            arg = text[len("/mode"):].strip()
            if arg in ("approve", "auto"):
                self.state.data["mode"] = arg
                self.state.save()
                self.api.send(user, f"Режим: <b>{arg}</b>.")
            else:
                self.api.send(user, f"Сейчас: <b>{self.mode}</b>. Укажите /mode approve или /mode auto.")
        elif msg.get("reply_to_message"):
            self.on_edit(msg)
        else:
            self.api.send(user, "Команды: /status, /mode approve, /mode auto. "
                                "Чтобы поправить черновик, ответьте на него своим текстом.")

    def on_edit(self, msg: dict):
        replied = msg["reply_to_message"]["message_id"]
        for post in self.queue():
            st = self.state.data["posts"].get(post.id, {})
            if st.get("draft_msg_id") == replied and st.get("status") == "sent":
                st["text_override"] = entities_to_html(msg.get("text", ""), msg.get("entities", []))
                self.api.drop_buttons(self.owner, replied)
                self.send_for_approval(post, self.problems(post), "исправленный вариант")
                return
        self.api.send(self.owner, "Не нашёл черновик, на который вы ответили.")

    def on_button(self, cb: dict):
        if cb.get("from", {}).get("id") != self.owner:
            self.api.answer(cb["id"])
            return
        action, _, post_id = (cb.get("data") or "").partition(":")
        post = next((p for p in self.queue() if p.id == post_id), None)
        st = self.state.data["posts"].get(post_id, {})
        msg = cb.get("message", {})

        if action == "del":
            if st.get("channel_msg_id"):
                self.api.delete(self.cfg.channel, st["channel_msg_id"])
                st["status"] = "rejected"
                self.state.save()
                self.api.answer(cb["id"], "Удалено из канала")
            else:
                self.api.answer(cb["id"], "Не нашёл сообщение в канале")
            self.api.drop_buttons(self.owner, msg.get("message_id"))
            return

        if post is None or st.get("status") != "sent":
            self.api.answer(cb["id"], "Этот черновик уже обработан")
            return
        if action == "pub":
            self.api.drop_buttons(self.owner, msg.get("message_id"))
            self.publish(post)
            self.api.answer(cb["id"], "Опубликовано")
        elif action == "later":
            st.update(status=None, postpone_until=(self.now().date() + timedelta(days=1)).isoformat())
            self.state.save()
            self.api.drop_buttons(self.owner, msg.get("message_id"))
            self.api.answer(cb["id"], "Пришлю в следующий день постов")
        elif action == "drop":
            st["status"] = "rejected"
            self.state.save()
            self.api.drop_buttons(self.owner, msg.get("message_id"))
            self.api.answer(cb["id"], "Убрал в корзину")

    def status(self) -> str:
        today = self.now().date()
        posts = self.state.data["posts"].values()
        published = sum(1 for p in posts if p.get("status") == "published")
        waiting = sum(1 for p in posts if p.get("status") == "sent")
        days = ", ".join(DAY_NAMES[d] for d in self.cfg.slot_days)
        return (f"Режим: <b>{self.mode}</b>\nКанал: {html.escape(self.cfg.channel)}\n"
                f"В очереди: {len(self.pending(today))}\nЖдут вашего решения: {waiting}\n"
                f"Опубликовано: {published}\nПосты: {days} в {self.cfg.slot_hour}:00 МСК")

    # ---------- цикл ----------

    def run(self):
        log.info("бот запущен, режим %s", self.mode)
        while True:
            try:
                for upd in self.api.updates(self.state.data.get("offset")):
                    self.state.data["offset"] = upd["update_id"] + 1
                    self.on_update(upd)
                    self.state.save()
                self.tick()
            except (TelegramError, OSError) as exc:
                log.warning("ошибка: %s", exc)
                time.sleep(10)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(argv) >= 2 and argv[0] == "check":
        files = argv[1:]
        if files[0] == "--rate":
            rate, files = float(files[1].replace(",", ".")), files[2:]
        else:
            rate = fetch_key_rate()
        bad = 0
        for name in files:
            problems = check_post(parse_post(Path(name)), rate)
            print(f"{name}: {'OK' if not problems else '; '.join(problems)}")
            bad += bool(problems)
        return 1 if bad else 0
    if argv == ["run"]:
        cfg = Config.from_env()
        Kanal(cfg, Telegram(cfg.token), State(cfg.state_file)).run()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
