import importlib.util
import logging
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "main.py"


def load_app():
    """Import the script without reading local secrets or opening the log file."""
    spec = importlib.util.spec_from_file_location("fl_monitor_main", MODULE_PATH)
    app = importlib.util.module_from_spec(spec)
    assert spec.loader is not None

    fake_dotenv = types.SimpleNamespace(load_dotenv=lambda *_args, **_kwargs: None)
    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch.dict(sys.modules, {"dotenv": fake_dotenv}):
            with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
                spec.loader.exec_module(app)

    return app


app = load_app()


class FilteringTests(unittest.TestCase):
    def test_whole_word_keywords_do_not_match_inside_larger_words(self):
        hits = app.count_matches(
            "Нужен email шаблон и premium landing page",
            ["ml", "ui", "email"],
        )

        self.assertNotIn("ml", hits)
        self.assertNotIn("ui", hits)
        self.assertIn("email", hits)

    def test_whole_word_keywords_match_standalone_tokens(self):
        hits = app.count_matches(
            "Нужен python ML pipeline и UI для админки",
            ["python", "ml", "ui"],
        )

        self.assertEqual(["python", "ml", "ui"], hits)

    def test_two_hard_anti_keywords_force_reject_even_with_core_hits(self):
        verdict, confidence, score = app.verdict_and_confidence(
            core_hits=["python", "api", "telegram"],
            anti_hits=["дизайн", "figma"],
            text_len=1200,
        )

        self.assertEqual("❌ НЕ РЕЛЕВАНТНО", verdict)
        self.assertEqual(0.10, confidence)
        self.assertEqual(-10, score)

    def test_title_anti_skips_marketing_orders_before_page_fetch(self):
        self.assertTrue(app.title_is_anti("SMM продвижение канала"))
        self.assertFalse(app.title_is_anti("Python парсер для API"))

    def test_telegram_keyword_marks_short_task_as_likely_doable(self):
        doable, reason = app.can_do_1_2_days(
            "Нужен telegram bot для уведомлений",
            core_hits=["telegram", "бот"],
        )

        self.assertEqual("скорее да", doable)
        self.assertIn("1", reason)


class RssParsingTests(unittest.TestCase):
    def test_fetch_rss_items_unescapes_html_entities_and_strips_markup(self):
        rss = """<?xml version="1.0" encoding="utf-8"?>
        <rss>
          <channel>
            <item>
              <title>Python &amp; API интеграция</title>
              <link>https://www.fl.ru/projects/123/test/</link>
              <description>&lt;p&gt;Нужен &lt;b&gt;бот&lt;/b&gt;&amp;nbsp;для Telegram&lt;/p&gt;</description>
            </item>
          </channel>
        </rss>
        """

        with mock.patch.object(app, "http_get", return_value=(rss, 200)):
            items = app.fetch_rss_items()

        self.assertEqual(1, len(items))
        self.assertEqual("Python & API интеграция", items[0]["title"])
        self.assertEqual("https://www.fl.ru/projects/123/test/", items[0]["link"])
        self.assertEqual("Нужен бот для Telegram", items[0]["desc"])


class BuildFullMessageTests(unittest.TestCase):
    def test_irrelevant_verdict_does_not_include_reply_draft(self):
        message, verdict = app.build_full_message(
            title="Нужен логотип и дизайн в Figma",
            url="https://www.fl.ru/projects/123/example/",
            project_text="логотип дизайн figma ui ux",
            rss_desc="Короткое описание из RSS",
            used_fallback=True,
            source_label="RSS-лента fl.ru",
        )

        self.assertEqual("❌ НЕ РЕЛЕВАНТНО", verdict)
        self.assertIn("Анализ по RSS-ленте", message)
        self.assertNotIn("✉️ Отклик (черновик)", message)
        self.assertNotIn("Могу взять в работу", message)

    def test_relevant_verdict_includes_reply_draft(self):
        message, verdict = app.build_full_message(
            title="Python API интеграция с Telegram",
            url="https://www.fl.ru/projects/456/example/",
            project_text=(
                "Нужен python backend скрипт для интеграции API, telegram бот, "
                "sqlite база и автоматизация процессов."
            ),
            rss_desc="",
            used_fallback=False,
            source_label="главная fl.ru/projects/",
        )

        self.assertEqual("✅ БРАТЬ", verdict)
        self.assertIn("✉️ Отклик (черновик)", message)
        self.assertIn("Могу взять в работу", message)


class ClaudeAnalyzeTests(unittest.TestCase):
    def test_missing_api_key_allows_order_without_external_call(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=True):
            with mock.patch.dict(sys.modules, {"anthropic": None}):
                self.assertTrue(app.claude_analyze("Python парсер", "Нужен парсер"))

    def test_api_no_response_rejects_order(self):
        created = {}

        class FakeMessages:
            def create(self, **kwargs):
                created.update(kwargs)
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text="NO")]
                )

        class FakeAnthropicClient:
            def __init__(self, api_key):
                self.api_key = api_key
                self.messages = FakeMessages()

        fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
            with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
                self.assertFalse(app.claude_analyze("Логотип", "Нужен дизайн"))

        self.assertEqual("claude-haiku-4-5-20251001", created["model"])
        self.assertEqual(10, created["max_tokens"])
        self.assertIn("Ответь строго одним словом", created["messages"][0]["content"])

    def test_api_exception_allows_order_to_avoid_silent_drop(self):
        class FailingAnthropicClient:
            def __init__(self, api_key):
                self.messages = self

            def create(self, **_kwargs):
                raise RuntimeError("temporary API failure")

        fake_anthropic = types.SimpleNamespace(Anthropic=FailingAnthropicClient)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
            with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
                self.assertTrue(app.claude_analyze("Python API", "Нужен backend"))


class HttpGetTests(unittest.TestCase):
    def test_success_forces_utf8_encoding_and_adds_cache_buster(self):
        calls = []

        class FakeResponse:
            status_code = 200
            encoding = None

            @property
            def text(self):
                return f"decoded-as-{self.encoding}"

            def raise_for_status(self):
                return None

        response = FakeResponse()

        def fake_get(url, headers, timeout):
            calls.append((url, headers, timeout))
            return response

        with mock.patch.object(app.requests, "get", side_effect=fake_get):
            with mock.patch.object(app.time, "time", return_value=1234567890):
                text, code = app.http_get("https://example.test/projects/")

        self.assertEqual(("decoded-as-utf-8", 200), (text, code))
        self.assertEqual("utf-8", response.encoding)
        self.assertEqual(
            "https://example.test/projects/?_cb=1234567890", calls[0][0]
        )
        self.assertIs(calls[0][1], app.HEADERS)
        self.assertEqual(app.HTTP_TIMEOUT, calls[0][2])

    def test_403_returns_immediately_without_retrying(self):
        class ForbiddenResponse:
            status_code = 403
            text = "forbidden"

            def raise_for_status(self):
                raise AssertionError("403 should return before raise_for_status")

        with mock.patch.object(app.requests, "get", return_value=ForbiddenResponse()) as get:
            with mock.patch.object(app.time, "sleep") as sleep:
                text, code = app.http_get("https://example.test/projects/")

        self.assertEqual(("", 403), (text, code))
        self.assertEqual(1, get.call_count)
        sleep.assert_not_called()


class EmailSendTests(unittest.TestCase):
    def test_missing_email_config_returns_false_without_smtp(self):
        empty_email_env = {
            "EMAIL_TO": "",
            "EMAIL_FROM": "",
            "EMAIL_PASSWORD": "",
            "EMAIL_SMTP": "",
            "EMAIL_PORT": "465",
        }

        with mock.patch.dict(os.environ, empty_email_env, clear=True):
            with mock.patch.object(app.smtplib, "SMTP_SSL") as smtp_ssl:
                self.assertFalse(app.email_send("body"))

        smtp_ssl.assert_not_called()

    def test_port_587_uses_starttls_before_login_and_send(self):
        events = []

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                events.append(("connect", host, port, timeout))

            def __enter__(self):
                events.append(("enter",))
                return self

            def __exit__(self, exc_type, exc, tb):
                events.append(("exit", exc_type))

            def starttls(self):
                events.append(("starttls",))

            def login(self, user, password):
                events.append(("login", user, password))

            def sendmail(self, from_addr, to_addr, message):
                events.append(("sendmail", from_addr, to_addr, message))

        email_env = {
            "EMAIL_TO": "client@example.test",
            "EMAIL_FROM": "monitor@example.test",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_SMTP": "smtp.example.test",
            "EMAIL_PORT": "587",
        }

        with mock.patch.dict(os.environ, email_env, clear=True):
            with mock.patch.object(app.smtplib, "SMTP", FakeSMTP):
                with mock.patch.object(app.smtplib, "SMTP_SSL") as smtp_ssl:
                    self.assertTrue(app.email_send("Тестовое сообщение"))

        self.assertEqual(
            [
                ("connect", "smtp.example.test", 587, 30),
                ("enter",),
                ("starttls",),
                ("login", "monitor@example.test", "secret"),
            ],
            events[:4],
        )
        self.assertEqual("sendmail", events[4][0])
        self.assertEqual("monitor@example.test", events[4][1])
        self.assertEqual("client@example.test", events[4][2])
        self.assertIn(b"Subject:", events[4][3])
        self.assertIn(b"From: monitor@example.test", events[4][3])
        self.assertIn(b"To: client@example.test", events[4][3])
        self.assertEqual(("exit", None), events[5])
        smtp_ssl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
