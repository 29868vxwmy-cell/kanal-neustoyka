import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from bot.kanal import MSK, Config, Kanal, State, entities_to_html
from bot.posts import PostError, check_post, parse_key_rate, parse_post, rate_mentions

ROOT = Path(__file__).resolve().parent.parent
CALC = '<a href="https://kalkulyator-neustoyki.ru/?utm_source=telegram">Посчитать</a>'
SRC = '<a href="https://www.cbr.ru/press/keypr/">ЦБ</a>'
BODY = "Текст поста про неустойку и долги по УПД. " * 10


def write_post(folder: Path, pid: str, text: str, extra: str = "") -> Path:
    path = folder / f"{pid}.md"
    path.write_text(f"---\nid: {pid}\n{extra}---\n{text}\n", encoding="utf-8")
    return path


class FakeApi:
    def __init__(self):
        self.sent, self.deleted, self.answers = [], [], []
        self.next_id = 100

    def send(self, chat_id, text, buttons=None, reply_to=None):
        self.next_id += 1
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons, "id": self.next_id})
        return {"message_id": self.next_id}

    def answer(self, cb_id, text=""):
        self.answers.append(text)

    def drop_buttons(self, chat_id, message_id):
        pass

    def delete(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class PostsTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_parse_with_sources(self):
        p = parse_post(write_post(self.dir, "a-post", "Текст", "sources:\n  - https://cbr.ru/x\n"))
        self.assertEqual(p.id, "a-post")
        self.assertEqual(p.sources, ["https://cbr.ru/x"])

    def test_bad_id_rejected(self):
        with self.assertRaises(PostError):
            parse_post(write_post(self.dir, "Пост", "Текст"))

    def test_clean_post_passes(self):
        p = parse_post(write_post(self.dir, "ok", f"{BODY}\n{SRC}\n{CALC}"))
        self.assertEqual(check_post(p, 14.0), [])

    def test_missing_source_and_calc(self):
        problems = check_post(parse_post(write_post(self.dir, "bad", BODY)), 14.0)
        self.assertTrue(any("источник" in x for x in problems))
        self.assertTrue(any("калькулятор" in x for x in problems))

    def test_foreign_link_and_promise(self):
        text = f'{BODY} Гарантированно вернём. <a href="https://evil.example/">тут</a> {SRC} {CALC}'
        problems = check_post(parse_post(write_post(self.dir, "x1", text)), 14.0)
        self.assertTrue(any("посторонний" in x for x in problems))
        self.assertTrue(any("гарантированно" in x for x in problems))

    def test_wrong_key_rate(self):
        text = f"{BODY} Ключевая ставка 16% годовых. {SRC} {CALC}"
        problems = check_post(parse_post(write_post(self.dir, "x2", text)), 14.0)
        self.assertTrue(any("16%" in x for x in problems))

    def test_key_rate_unknown(self):
        text = f"{BODY} ключевую ставку 14% {SRC} {CALC}"
        problems = check_post(parse_post(write_post(self.dir, "x3", text)), None)
        self.assertTrue(any("ставку ЦБ" in x for x in problems))

    def test_rate_mentions(self):
        self.assertEqual(rate_mentions("сохранил ключевую ставку на уровне 14,00% годовых"), [14.0])
        self.assertEqual(rate_mentions("пеня 0,1% в день"), [])

    def test_parse_key_rate(self):
        page = '<tr><td>24.10.2026</td><td>14,00</td></tr><tr><td>23.10.2026</td><td>14,00</td>'
        self.assertEqual(parse_key_rate(page), 14.0)
        self.assertIsNone(parse_key_rate("<html>другая вёрстка</html>"))

    def test_starter_posts_parse(self):
        posts = [parse_post(p) for p in sorted((ROOT / "posts" / "queue").glob("*.md"))]
        self.assertGreaterEqual(len(posts), 10)
        for p in posts:
            self.assertNotIn("Ключевая ставка 16", p.text)


class EntitiesTest(unittest.TestCase):
    def test_bold_and_link(self):
        text = "Жирно и ссылка"
        ents = [{"type": "bold", "offset": 0, "length": 5},
                {"type": "text_link", "offset": 8, "length": 6, "url": "https://cbr.ru/?a=1&b=2"}]
        self.assertEqual(entities_to_html(text, ents),
                         '<b>Жирно</b> и <a href="https://cbr.ru/?a=1&amp;b=2">ссылка</a>')

    def test_emoji_offsets(self):
        text = "👉 <тут>"
        ents = [{"type": "bold", "offset": 3, "length": 5}]
        self.assertEqual(entities_to_html(text, ents), "👉 <b>&lt;тут&gt;</b>")


class KanalTest(unittest.TestCase):
    def make(self, mode="approve", when=datetime(2026, 10, 26, 10, 5, tzinfo=MSK)):  # понедельник
        repo = Path(tempfile.mkdtemp())
        (repo / "posts" / "queue").mkdir(parents=True)
        self.queue = repo / "posts" / "queue"
        cfg = Config(token="t", channel="@kanal", mode=mode, bind_code="secret",
                     repo_dir=repo, state_file=repo / "state.json")
        self.api = FakeApi()
        self.clock = [when]
        k = Kanal(cfg, self.api, State(cfg.state_file), key_rate=lambda: 14.0,
                  now=lambda: self.clock[0])
        return k

    def bind(self, k):
        k.on_update({"message": {"from": {"id": 7}, "chat": {"type": "private", "id": 7},
                                 "text": "/start secret"}})
        self.api.sent.clear()

    def test_binding_needs_code(self):
        k = self.make()
        k.on_update({"message": {"from": {"id": 9}, "chat": {"type": "private"}, "text": "/start"}})
        self.assertIsNone(k.owner)
        self.bind(k)
        self.assertEqual(k.owner, 7)

    def test_nothing_before_binding(self):
        k = self.make()
        write_post(self.queue, "p1", f"{BODY}{SRC}{CALC}")
        k.tick()
        self.assertEqual(self.api.sent, [])

    def test_approve_flow(self):
        k = self.make()
        self.bind(k)
        write_post(self.queue, "p1", f"{BODY}{SRC}{CALC}")
        k.tick()
        draft = self.api.sent[-1]
        self.assertEqual(draft["chat"], 7)
        self.assertEqual(draft["buttons"][0][0][1], "pub:p1")
        k.tick()  # второй такт в тот же день ничего не шлёт
        self.assertEqual(len([m for m in self.api.sent if m["buttons"]]), 1)

        k.on_update({"callback_query": {"id": "c", "from": {"id": 7}, "data": "pub:p1",
                                        "message": {"message_id": draft["id"]}}})
        self.assertTrue(any(m["chat"] == "@kanal" for m in self.api.sent))
        self.assertEqual(k.state.data["posts"]["p1"]["status"], "published")

    def test_stranger_button_ignored(self):
        k = self.make()
        self.bind(k)
        write_post(self.queue, "p1", f"{BODY}{SRC}{CALC}")
        k.tick()
        k.on_update({"callback_query": {"id": "c", "from": {"id": 666}, "data": "pub:p1",
                                        "message": {"message_id": 1}}})
        self.assertFalse(any(m["chat"] == "@kanal" for m in self.api.sent))

    def test_auto_publishes_clean_post(self):
        k = self.make(mode="auto")
        self.bind(k)
        write_post(self.queue, "p1", f"{BODY}{SRC}{CALC}")
        k.tick()
        self.assertEqual(self.api.sent[0]["chat"], "@kanal")
        self.assertEqual(self.api.sent[1]["buttons"][0][0][1], "del:p1")

    def test_auto_sends_failed_post_to_owner(self):
        k = self.make(mode="auto")
        self.bind(k)
        write_post(self.queue, "p1", BODY)
        k.tick()
        self.assertFalse(any(m["chat"] == "@kanal" for m in self.api.sent))
        self.assertIn("Автопроверка не пройдена", self.api.sent[0]["text"])

    def test_no_post_on_off_day_and_before_hour(self):
        k = self.make(when=datetime(2026, 10, 27, 12, 0, tzinfo=MSK))  # вторник
        self.bind(k)
        write_post(self.queue, "p1", f"{BODY}{SRC}{CALC}")
        k.tick()
        self.assertEqual(self.api.sent, [])
        self.clock[0] = datetime(2026, 10, 28, 9, 0, tzinfo=MSK)  # среда, рано
        k.tick()
        self.assertEqual(self.api.sent, [])

    def test_empty_queue_warns_owner(self):
        k = self.make()
        self.bind(k)
        k.tick()
        self.assertIn("нет готового черновика", self.api.sent[0]["text"])

    def test_edit_by_reply(self):
        k = self.make()
        self.bind(k)
        write_post(self.queue, "p1", f"{BODY}{SRC}{CALC}")
        k.tick()
        draft = self.api.sent[-1]
        k.on_update({"message": {"from": {"id": 7}, "chat": {"type": "private"},
                                 "text": "Новый текст", "entities": [],
                                 "reply_to_message": {"message_id": draft["id"]}}})
        self.assertEqual(self.api.sent[-1]["text"], "Новый текст")
        self.assertEqual(k.state.data["posts"]["p1"]["draft_msg_id"], self.api.sent[-1]["id"])

    def test_mode_command(self):
        k = self.make()
        self.bind(k)
        k.on_update({"message": {"from": {"id": 7}, "chat": {"type": "private"},
                                 "text": "/mode auto"}})
        self.assertEqual(k.mode, "auto")

    def test_delete_from_channel(self):
        k = self.make(mode="auto")
        self.bind(k)
        write_post(self.queue, "p1", f"{BODY}{SRC}{CALC}")
        k.tick()
        k.on_update({"callback_query": {"id": "c", "from": {"id": 7}, "data": "del:p1",
                                        "message": {"message_id": 5}}})
        self.assertEqual(self.api.deleted, [("@kanal", self.api.sent[0]["id"])])

    def test_urgent_goes_out_off_schedule(self):
        k = self.make(when=datetime(2026, 10, 27, 12, 0, tzinfo=MSK))
        self.bind(k)
        write_post(self.queue, "news", f"{BODY}{SRC}{CALC}", "urgent: true\n")
        k.tick()
        self.assertEqual(self.api.sent[-1]["buttons"][0][0][1], "pub:news")


if __name__ == "__main__":
    unittest.main()
