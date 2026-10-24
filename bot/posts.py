"""Черновики постов: чтение из posts/queue и автопроверка перед публикацией."""

from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

# Домены, на которые можно ссылаться как на источник нормы или цифры.
SOURCE_DOMAINS = (
    "cbr.ru",
    "pravo.gov.ru",
    "vsrf.ru",
    "kad.arbitr.ru",
    "consultant.ru",
    "garant.ru",
    "nalog.gov.ru",
    "minfin.gov.ru",
    "pravo.ppt.ru",
    "ppt.ru",
    "rulaws.ru",
)
SITE_DOMAIN = "kalkulyator-neustoyki.ru"

# Обещания, которых юридический канал давать не должен.
FORBIDDEN = (
    "гарантированно",
    "гарантируем",
    "100%",
    "точно выиграете",
    "выиграете суд",
    "без риска",
)

TELEGRAM_LIMIT = 4096
MIN_LENGTH = 300

ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote"}


@dataclass
class Post:
    id: str
    text: str
    path: Path
    publish_on: date | None = None
    urgent: bool = False
    topic: str = ""
    sources: list[str] = field(default_factory=list)


class PostError(ValueError):
    pass


def parse_post(path: Path) -> Post:
    """Файл: блок `---` с полями, затем текст поста в HTML-разметке Telegram."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise PostError(f"{path.name}: нет заголовка ---")
    try:
        head, body = raw[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise PostError(f"{path.name}: заголовок не закрыт ---") from exc

    meta: dict[str, str] = {}
    sources: list[str] = []
    current = None
    for line in head.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and current == "sources":
            sources.append(line[4:].strip())
            continue
        key, _, value = line.partition(":")
        current = key.strip()
        meta[current] = value.strip()

    publish_on = None
    if meta.get("publish_on"):
        try:
            publish_on = date.fromisoformat(meta["publish_on"])
        except ValueError as exc:
            raise PostError(f"{path.name}: неверная дата publish_on") from exc

    post_id = meta.get("id") or path.stem
    # id уходит в кнопки Telegram (callback_data до 64 байт), поэтому латиница и короткий.
    if not re.fullmatch(r"[a-z0-9-]{2,50}", post_id):
        raise PostError(f"{path.name}: id только из a-z, 0-9 и дефиса, до 50 знаков")

    return Post(
        id=post_id,
        text=body.strip(),
        path=path,
        publish_on=publish_on,
        urgent=meta.get("urgent", "").lower() in ("true", "yes", "да"),
        topic=meta.get("topic", ""),
        sources=sources,
    )


def load_queue(queue_dir: Path) -> tuple[list[Post], list[str]]:
    """Черновики по порядку и ошибки в тех файлах, что не прочитались."""
    posts, errors = [], []
    for path in sorted(queue_dir.glob("*.md")):
        try:
            posts.append(parse_post(path))
        except PostError as exc:
            errors.append(str(exc))
    return sorted(posts, key=lambda p: (p.publish_on or date.min, p.id)), errors


def _host_ok(url: str, domains: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def links(text: str) -> list[str]:
    return [html.unescape(u) for u in re.findall(r'href="([^"]+)"', text)]


def visible_text(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def rate_mentions(text: str) -> list[float]:
    """Проценты, стоящие рядом со словами «ключевая ставка»."""
    plain = visible_text(text).lower()
    found = []
    for m in re.finditer(r"ключев\w*\s+ставк\w*", plain):
        window = plain[m.end() : m.end() + 60]
        for num in re.findall(r"(\d{1,2}(?:[.,]\d{1,2})?)\s?%", window):
            found.append(float(num.replace(",", ".")))
    return found


def check_post(post: Post, key_rate: float | None) -> list[str]:
    """Пустой список — пост можно публиковать без человека."""
    problems = []
    plain = visible_text(post.text)

    if len(plain) < MIN_LENGTH:
        problems.append(f"слишком короткий: {len(plain)} знаков")
    if len(post.text) > TELEGRAM_LIMIT:
        problems.append(f"длиннее лимита Telegram: {len(post.text)} > {TELEGRAM_LIMIT}")

    for tag in set(re.findall(r"</?([a-zA-Z0-9]+)", post.text)):
        if tag.lower() not in ALLOWED_TAGS:
            problems.append(f"тег <{tag}> Telegram не поддерживает")

    urls = links(post.text) + post.sources
    if not any(_host_ok(u, SOURCE_DOMAINS) for u in urls):
        problems.append("нет ссылки на источник из белого списка")
    if not any(_host_ok(u, (SITE_DOMAIN,)) for u in links(post.text)):
        problems.append("нет ссылки на калькулятор")
    for u in links(post.text):
        if not _host_ok(u, SOURCE_DOMAINS + (SITE_DOMAIN,)):
            problems.append(f"ссылка на посторонний сайт: {u}")

    lowered = plain.lower()
    for word in FORBIDDEN:
        if word in lowered:
            problems.append(f"запрещённая формулировка: «{word}»")

    mentioned = rate_mentions(post.text)
    if mentioned:
        if key_rate is None:
            problems.append("не удалось узнать ставку ЦБ, цифру ставки не проверить")
        else:
            for value in mentioned:
                if abs(value - key_rate) > 0.001:
                    problems.append(
                        f"в посте ставка {value:g}%, а на сайте ЦБ {key_rate:g}%"
                    )
    return problems


KEY_RATE_URL = "https://www.cbr.ru/hd_base/KeyRate/"


def fetch_key_rate(timeout: float = 20) -> float | None:
    """Текущая ключевая ставка с сайта ЦБ; None, если страница не разобралась."""
    req = urllib.request.Request(KEY_RATE_URL, headers={"User-Agent": "kanal-neustoyka"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = resp.read().decode("utf-8", "replace")
    except OSError:
        return None
    return parse_key_rate(page)


def parse_key_rate(page: str) -> float | None:
    # Таблица на странице: <td>23.10.2026</td><td>14,00</td>, свежая дата первой.
    # Если ЦБ поменяет вёрстку, вернётся None и пост со ставкой уйдёт на одобрение.
    m = re.search(
        r"<td[^>]*>\s*\d{2}\.\d{2}\.\d{4}\s*</td>\s*<td[^>]*>\s*(\d{1,2},\d{2})\s*</td>", page
    )
    return float(m.group(1).replace(",", ".")) if m else None
