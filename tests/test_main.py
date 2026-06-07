import importlib.util
import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MAIN_PATH = os.path.join(ROOT_DIR, "src", "main.py")


def load_main_module():
    spec = importlib.util.spec_from_file_location("fl_monitor_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch("logging.FileHandler", return_value=logging.NullHandler()):
        spec.loader.exec_module(module)
    return module


main = load_main_module()


class FakeResponse:
    def __init__(self, status_code=200, text_by_encoding=None):
        self.status_code = status_code
        self.encoding = None
        self.text_by_encoding = text_by_encoding or {"utf-8": "ok"}
        self.raise_for_status = Mock()

    @property
    def text(self):
        return self.text_by_encoding.get(self.encoding, "wrong encoding")


class MainModuleTests(unittest.TestCase):
    def test_http_get_forces_utf8_before_reading_response_text(self):
        response = FakeResponse(
            text_by_encoding={
                "utf-8": "Привет, мир",
                "windows-1251": "РџСЂРёРІРµС‚",
            }
        )

        with patch.object(main.time, "time", return_value=123), \
             patch.object(main.requests, "get", return_value=response) as get:
            text, status = main.http_get("https://example.test/projects/")

        self.assertEqual(text, "Привет, мир")
        self.assertEqual(status, 200)
        self.assertEqual(response.encoding, "utf-8")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://example.test/projects/?_cb=123")

    def test_http_get_returns_403_without_retrying(self):
        response = FakeResponse(status_code=403)

        with patch.object(main.requests, "get", return_value=response) as get, \
             patch.object(main.time, "sleep") as sleep:
            text, status = main.http_get("https://example.test/projects/")

        self.assertEqual((text, status), ("", 403))
        get.assert_called_once()
        response.raise_for_status.assert_not_called()
        sleep.assert_not_called()

    def test_build_full_message_omits_reply_draft_for_irrelevant_project(self):
        message, verdict = main.build_full_message(
            "Логотип и дизайн в Figma",
            "https://www.fl.ru/projects/100/",
            "Нужен логотип, дизайн, figma и фирменный стиль для рекламы.",
            "",
            used_fallback=False,
            source_label="главная fl.ru/projects/",
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertNotIn("✉️ Отклик (черновик)", message)

    def test_build_full_message_includes_reply_draft_for_relevant_project(self):
        message, verdict = main.build_full_message(
            "Python бот для Telegram",
            "https://www.fl.ru/projects/101/",
            "Нужен python telegram бот, api интеграция, sql база и автоматизация.",
            "",
            used_fallback=False,
            source_label="главная fl.ru/projects/",
        )

        self.assertEqual(verdict, "✅ БРАТЬ")
        self.assertIn("✉️ Отклик (черновик)", message)

    def test_claude_analyze_allows_project_when_api_key_is_missing(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertTrue(main.claude_analyze("Python parser", "Need parser"))

    def test_claude_analyze_rejects_no_response(self):
        fake_client = Mock()
        fake_client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="NO")]
        )
        fake_anthropic = SimpleNamespace(Anthropic=Mock(return_value=fake_client))

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            self.assertFalse(main.claude_analyze("Logo", "Need logo design"))

        fake_anthropic.Anthropic.assert_called_once_with(api_key="test-key")
        fake_client.messages.create.assert_called_once()

    def test_main_claude_rejection_marks_processed_without_sending_project(self):
        item = {
            "title": "Python parser",
            "link": "https://www.fl.ru/projects/200/test/",
            "desc": "RSS description",
        }

        with patch.object(main, "db_connect", return_value=object()), \
             patch.object(main, "fetch_projects_page", return_value=[item]), \
             patch.object(main, "fetch_rss_items") as fetch_rss_items, \
             patch.object(main, "is_processed", return_value=False), \
             patch.object(main, "http_get", return_value=("<html>project</html>", 200)), \
             patch.object(main, "try_extract_project_text", return_value="python parser automation " * 10), \
             patch.object(main, "claude_analyze", return_value=False), \
             patch.object(main, "build_full_message") as build_full_message, \
             patch.object(main, "tg_send", return_value=True) as tg_send, \
             patch.object(main, "email_send") as email_send, \
             patch.object(main, "mark_processed") as mark_processed, \
             patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        fetch_rss_items.assert_not_called()
        self.assertEqual(tg_send.call_count, 1)  # startup notification only
        email_send.assert_not_called()
        build_full_message.assert_not_called()
        mark_processed.assert_called_once_with(
            unittest.mock.ANY, "200", item["link"], item["title"]
        )

    def test_main_uses_email_fallback_when_telegram_delivery_fails(self):
        item = {
            "title": "Python Telegram bot",
            "link": "https://www.fl.ru/projects/201/test/",
            "desc": "RSS description",
        }

        with patch.object(main, "db_connect", return_value=object()), \
             patch.object(main, "fetch_projects_page", return_value=[item]), \
             patch.object(main, "fetch_rss_items") as fetch_rss_items, \
             patch.object(main, "is_processed", return_value=False), \
             patch.object(main, "http_get", return_value=("<html>project</html>", 200)), \
             patch.object(main, "try_extract_project_text", return_value="python telegram bot api sql automation " * 10), \
             patch.object(main, "claude_analyze", return_value=True), \
             patch.object(main, "tg_send", side_effect=[True, False]) as tg_send, \
             patch.object(main, "email_send", return_value=True) as email_send, \
             patch.object(main, "mark_processed") as mark_processed, \
             patch.object(main.time, "sleep", side_effect=KeyboardInterrupt), \
             patch.object(main, "MAX_PER_CYCLE", 1):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        fetch_rss_items.assert_not_called()
        self.assertEqual(tg_send.call_count, 2)
        email_send.assert_called_once()
        mark_processed.assert_called_once_with(
            unittest.mock.ANY, "201", item["link"], item["title"]
        )


if __name__ == "__main__":
    unittest.main()
