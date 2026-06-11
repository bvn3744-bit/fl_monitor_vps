import importlib.util
import logging
import os
import sys
import types
import unittest
from unittest import mock


MODULE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "main.py"))


def load_main_module():
    module_name = "fl_monitor_main_under_test"
    sys.modules.pop(module_name, None)

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None

    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.dict(sys.modules, {"dotenv": fake_dotenv}), \
            mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
        spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self._text = text
        self.status_code = status_code
        self.encoding = None
        self.raise_for_status = mock.Mock()

    @property
    def text(self):
        if self.encoding == "utf-8":
            return self._text
        return "wrong-decoding"


class FakeClaudeMessage:
    def __init__(self, text):
        self.content = [types.SimpleNamespace(text=text)]


class MainTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main_module()

    def test_http_get_forces_utf8_and_adds_cache_buster(self):
        response = FakeResponse("Привет", 200)
        with mock.patch.object(self.main.time, "time", return_value=123), \
                mock.patch.object(self.main.time, "sleep"), \
                mock.patch.object(self.main.requests, "get", return_value=response) as get:
            text, status = self.main.http_get("https://example.test/projects/?page=1")

        self.assertEqual(text, "Привет")
        self.assertEqual(status, 200)
        self.assertEqual(response.encoding, "utf-8")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://example.test/projects/?page=1&_cb=123")

    def test_http_get_returns_immediately_on_403_without_retries(self):
        response = FakeResponse("", 403)
        with mock.patch.object(self.main.time, "time", return_value=123), \
                mock.patch.object(self.main.time, "sleep") as sleep, \
                mock.patch.object(self.main.requests, "get", return_value=response) as get:
            text, status = self.main.http_get("https://example.test/projects/")

        self.assertEqual((text, status), ("", 403))
        get.assert_called_once()
        sleep.assert_not_called()
        response.raise_for_status.assert_not_called()

    def test_fetch_rss_items_decodes_entities_and_strips_html(self):
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel><item>
            <title>Python &amp;amp; API бот</title>
            <link> https://www.fl.ru/projects/123/test/ </link>
            <description><![CDATA[<p>Нужен <b>бот</b>&nbsp;&amp;amp; интеграция</p><script>bad()</script>]]></description>
        </item></channel></rss>"""

        with mock.patch.object(self.main, "http_get", return_value=(rss, 200)):
            items = self.main.fetch_rss_items()

        self.assertEqual(items, [{
            "title": "Python & API бот",
            "link": "https://www.fl.ru/projects/123/test/",
            "desc": "Нужен бот & интеграция",
        }])

    def test_whole_word_keywords_do_not_match_inside_other_words(self):
        hits = self.main.count_matches("email builder with ui and api", ["ml", "ui", "api"])

        self.assertEqual(hits, ["ui", "api"])

    def test_hard_anti_keywords_override_otherwise_relevant_work(self):
        verdict, confidence, score = self.main.verdict_and_confidence(
            core_hits=["python", "api", "бот"],
            anti_hits=["дизайн", "figma"],
            text_len=1200,
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertEqual(confidence, 0.10)
        self.assertEqual(score, -10)

    def test_build_full_message_omits_reply_draft_for_irrelevant_project(self):
        message, verdict = self.main.build_full_message(
            "Логотип и дизайн",
            "https://www.fl.ru/projects/5/logo/",
            "Нужен логотип дизайн figma иллюстрация брендбук",
            "",
            False,
            "RSS-лента fl.ru",
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertNotIn("✉️ Отклик (черновик)", message)

    def test_claude_analyze_rejects_non_yes_response(self):
        fake_anthropic = types.ModuleType("anthropic")
        fake_client = mock.Mock()
        fake_client.messages.create.return_value = FakeClaudeMessage("NO")
        fake_anthropic.Anthropic = mock.Mock(return_value=fake_client)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True), \
                mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            result = self.main.claude_analyze("Дизайн логотипа", "Нужно сделать логотип")

        self.assertFalse(result)
        fake_anthropic.Anthropic.assert_called_once_with(api_key="test-key")

    def test_main_marks_claude_rejected_project_without_sending_notification(self):
        item = {
            "title": "Нужно сделать логотип",
            "link": "https://www.fl.ru/projects/10/logo/",
            "desc": "Нужно сделать логотип и дизайн",
        }

        with mock.patch.object(self.main, "tg_send", return_value=True) as tg_send, \
                mock.patch.object(self.main, "db_connect", return_value=mock.sentinel.conn), \
                mock.patch.object(self.main, "fetch_projects_page", return_value=[item]), \
                mock.patch.object(self.main, "is_processed", return_value=False), \
                mock.patch.object(self.main, "http_get", return_value=("", 0)), \
                mock.patch.object(self.main, "claude_analyze", return_value=False), \
                mock.patch.object(self.main, "mark_processed") as mark_processed, \
                mock.patch.object(self.main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.main.main()

        tg_send.assert_called_once()
        mark_processed.assert_called_once_with(
            mock.sentinel.conn,
            "10",
            "https://www.fl.ru/projects/10/logo/",
            "Нужно сделать логотип",
        )

    def test_main_uses_email_fallback_when_telegram_delivery_fails(self):
        item = {
            "title": "Python API бот",
            "link": "https://www.fl.ru/projects/20/bot/",
            "desc": "Нужен python api telegram бот для автоматизации",
        }

        with mock.patch.object(self.main, "tg_send", side_effect=[True, False]) as tg_send, \
                mock.patch.object(self.main, "email_send", return_value=True) as email_send, \
                mock.patch.object(self.main, "db_connect", return_value=mock.sentinel.conn), \
                mock.patch.object(self.main, "fetch_projects_page", return_value=[item]), \
                mock.patch.object(self.main, "is_processed", return_value=False), \
                mock.patch.object(self.main, "http_get", return_value=("", 0)), \
                mock.patch.object(self.main, "claude_analyze", return_value=True), \
                mock.patch.object(self.main, "mark_processed") as mark_processed, \
                mock.patch.object(self.main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.main.main()

        self.assertEqual(tg_send.call_count, 2)
        email_send.assert_called_once()
        mark_processed.assert_called_once_with(
            mock.sentinel.conn,
            "20",
            "https://www.fl.ru/projects/20/bot/",
            "Python API бот",
        )


if __name__ == "__main__":
    unittest.main()
