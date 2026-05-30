import importlib.util
import logging
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "main.py"


def load_main_module():
    module_name = "fl_monitor_main"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None

    with mock.patch(
        "logging.FileHandler",
        side_effect=lambda *args, **kwargs: logging.NullHandler(),
    ):
        spec.loader.exec_module(module)

    return module


class KeywordFilteringTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_whole_word_keywords_do_not_match_inside_other_words(self):
        hits = self.main.count_matches(
            "HTML шаблон и build pipeline, отдельно нужны ML и API",
            ["ml", "ui", "api"],
        )

        self.assertEqual(["ml", "api"], hits)

    def test_title_anti_blocks_marketing_orders_early(self):
        self.assertTrue(
            self.main.title_is_anti("Нужен маркетолог для продвижение канала")
        )
        self.assertFalse(self.main.title_is_anti("Нужен Telegram бот для уведомлений"))


class RssParsingTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_fetch_rss_items_strips_html_and_unescapes_entities(self):
        rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss><channel><item>
  <title>Python &amp; API интеграция</title>
  <link>https://www.fl.ru/projects/123/example/</link>
  <description><![CDATA[<p>Нужен&nbsp;<b>бот</b><br/>API</p><script>bad()</script>]]></description>
</item></channel></rss>"""

        with mock.patch.object(self.main, "http_get", return_value=(rss, 200)):
            items = self.main.fetch_rss_items()

        self.assertEqual(
            [
                {
                    "title": "Python & API интеграция",
                    "link": "https://www.fl.ru/projects/123/example/",
                    "desc": "Нужен бот API",
                }
            ],
            items,
        )

    def test_fetch_rss_items_returns_empty_list_for_malformed_xml(self):
        with mock.patch.object(self.main, "http_get", return_value=("<rss>", 200)):
            self.assertEqual([], self.main.fetch_rss_items())


class MessageCompositionTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_irrelevant_message_does_not_include_reply_draft(self):
        with mock.patch.object(
            self.main, "build_reply_draft", return_value="DRAFT SHOULD NOT APPEAR"
        ) as draft:
            message, verdict = self.main.build_full_message(
                "Need figma UI",
                "https://example.test/project",
                "figma ui ux",
                "RSS fallback text",
                True,
                "RSS-лента fl.ru",
            )

        self.assertEqual("❌ НЕ РЕЛЕВАНТНО", verdict)
        draft.assert_not_called()
        self.assertIn("Анализ по RSS-ленте", message)
        self.assertNotIn("✉️ Отклик (черновик)", message)
        self.assertNotIn("DRAFT SHOULD NOT APPEAR", message)

    def test_relevant_message_includes_reply_draft(self):
        with mock.patch.object(
            self.main, "build_reply_draft", return_value="DRAFT INCLUDED"
        ) as draft:
            message, verdict = self.main.build_full_message(
                "Python Telegram API bot",
                "https://example.test/project",
                "python telegram api bot automation parser",
                "",
                False,
                "главная fl.ru/projects/",
            )

        self.assertEqual("✅ БРАТЬ", verdict)
        draft.assert_called_once()
        self.assertIn("DRAFT INCLUDED", message)


class HttpGetTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_successful_response_is_forced_to_utf8_and_cache_busted(self):
        class FakeResponse:
            status_code = 200
            encoding = None

            @property
            def text(self):
                return f"decoded-as-{self.encoding}"

            def raise_for_status(self):
                return None

        response = FakeResponse()

        with mock.patch.object(self.main.time, "time", return_value=123):
            with mock.patch.object(
                self.main.requests, "get", return_value=response
            ) as get:
                text, status_code = self.main.http_get("https://example.test/feed")

        self.assertEqual(("decoded-as-utf-8", 200), (text, status_code))
        self.assertEqual("utf-8", response.encoding)
        self.assertEqual(
            "https://example.test/feed?_cb=123",
            get.call_args.args[0],
        )
        self.assertIs(get.call_args.kwargs["headers"], self.main.HEADERS)
        self.assertEqual(self.main.HTTP_TIMEOUT, get.call_args.kwargs["timeout"])

    def test_403_returns_without_retrying_or_sleeping(self):
        class ForbiddenResponse:
            status_code = 403
            text = ""

            def raise_for_status(self):
                raise AssertionError("403 responses should not be raised")

        with mock.patch.object(
            self.main.requests, "get", return_value=ForbiddenResponse()
        ) as get:
            with mock.patch.object(self.main.time, "sleep") as sleep:
                self.assertEqual(
                    ("", 403),
                    self.main.http_get("https://example.test/projects/"),
                )

        self.assertEqual(1, get.call_count)
        sleep.assert_not_called()


class EmailSendTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_missing_email_config_returns_false_without_smtp(self):
        empty_email_env = {
            "EMAIL_TO": "",
            "EMAIL_FROM": "",
            "EMAIL_PASSWORD": "",
            "EMAIL_SMTP": "",
            "EMAIL_PORT": "465",
        }

        with mock.patch.dict(os.environ, empty_email_env):
            with mock.patch.object(self.main.smtplib, "SMTP_SSL") as smtp_ssl:
                self.assertFalse(self.main.email_send("body"))

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

        with mock.patch.dict(os.environ, email_env):
            with mock.patch.object(self.main.smtplib, "SMTP", FakeSMTP):
                with mock.patch.object(self.main.smtplib, "SMTP_SSL") as smtp_ssl:
                    self.assertTrue(self.main.email_send("Тестовое сообщение"))

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


class ClaudeAnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_allows_project_when_anthropic_key_is_missing(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            with mock.patch.dict(sys.modules, {"anthropic": None}):
                self.assertTrue(self.main.claude_analyze("Parser task", "Build parser"))

    def test_returns_false_for_no_response_and_truncates_prompt(self):
        calls = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text="NO")]
                )

        class FakeAnthropicClient:
            def __init__(self, api_key):
                self.api_key = api_key
                self.messages = FakeMessages()

        fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)
        long_text = "x" * 2100

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
                self.assertFalse(self.main.claude_analyze("Logo task", long_text))

        self.assertEqual("claude-haiku-4-5-20251001", calls[0]["model"])
        self.assertEqual(10, calls[0]["max_tokens"])
        prompt = calls[0]["messages"][0]["content"]
        self.assertIn("x" * 2000, prompt)
        self.assertNotIn("x" * 2001, prompt)

    def test_allows_project_when_anthropic_call_fails(self):
        def raise_on_create(api_key):
            raise RuntimeError("network unavailable")

        fake_anthropic = types.SimpleNamespace(Anthropic=raise_on_create)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
                self.assertTrue(self.main.claude_analyze("Parser task", "Build parser"))


class MainLoopClaudeGateTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_claude_rejection_marks_processed_without_sending_project(self):
        conn = object()
        project = {
            "title": "Python parser",
            "link": "https://www.fl.ru/projects/123/example/",
            "desc": "rss description",
        }

        with mock.patch.object(self.main, "db_connect", return_value=conn), \
            mock.patch.object(self.main, "fetch_projects_page", return_value=[project]), \
            mock.patch.object(self.main, "fetch_rss_items") as fetch_rss_items, \
            mock.patch.object(self.main, "is_processed", return_value=False), \
            mock.patch.object(self.main, "http_get", return_value=("<html></html>", 200)), \
            mock.patch.object(
                self.main, "try_extract_project_text", return_value="python parser"
            ), \
            mock.patch.object(self.main, "claude_analyze", return_value=False), \
            mock.patch.object(self.main, "build_full_message") as build_full_message, \
            mock.patch.object(self.main, "mark_processed") as mark_processed, \
            mock.patch.object(self.main, "tg_send", return_value=True) as tg_send, \
            mock.patch.object(self.main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.main.main()

        fetch_rss_items.assert_not_called()
        build_full_message.assert_not_called()
        mark_processed.assert_called_once_with(
            conn,
            "123",
            "https://www.fl.ru/projects/123/example/",
            "Python parser",
        )
        self.assertEqual(1, tg_send.call_count)


if __name__ == "__main__":
    unittest.main()
