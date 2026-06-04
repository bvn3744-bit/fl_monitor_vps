# -*- coding: utf-8 -*-
import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "src" / "main.py"


def load_main_module():
    spec = importlib.util.spec_from_file_location("fl_monitor_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(logging, "FileHandler", return_value=logging.NullHandler()):
        spec.loader.exec_module(module)
    return module


main = load_main_module()


class HttpTests(unittest.TestCase):
    def test_http_get_cache_busts_and_forces_utf8(self):
        response = mock.Mock(status_code=200, text="Привет")

        with mock.patch.object(main.time, "time", return_value=1234567890), \
                mock.patch.object(main.time, "sleep"), \
                mock.patch.object(main.requests, "get", return_value=response) as get:
            text, status_code = main.http_get("https://example.test/path?x=1")

        self.assertEqual((text, status_code), ("Привет", 200))
        get.assert_called_once_with(
            "https://example.test/path?x=1&_cb=1234567890",
            headers=main.HEADERS,
            timeout=main.HTTP_TIMEOUT,
        )
        self.assertEqual(response.encoding, "utf-8")
        response.raise_for_status.assert_called_once_with()

    def test_http_get_returns_403_without_retrying_blocked_page(self):
        response = mock.Mock(status_code=403, text="blocked")

        with mock.patch.object(main.time, "time", return_value=111), \
                mock.patch.object(main.time, "sleep") as sleep, \
                mock.patch.object(main.requests, "get", return_value=response) as get:
            text, status_code = main.http_get("https://example.test/projects/")

        self.assertEqual((text, status_code), ("", 403))
        get.assert_called_once()
        response.raise_for_status.assert_not_called()
        sleep.assert_not_called()


class BuildFullMessageTests(unittest.TestCase):
    def test_relevant_message_includes_reply_draft(self):
        message, verdict = main.build_full_message(
            "Python telegram bot",
            "https://www.fl.ru/projects/123/test/",
            "Нужен python telegram бот для API интеграции и автоматизации.",
            "",
            False,
            "test source",
        )

        self.assertEqual(verdict, "✅ БРАТЬ")
        self.assertIn("✉️ Отклик (черновик)", message)
        self.assertIn("Здравствуйте!", message)

    def test_non_relevant_message_does_not_include_reply_draft(self):
        message, verdict = main.build_full_message(
            "Логотип и фирменный стиль",
            "https://www.fl.ru/projects/456/test/",
            "Нужен логотип, дизайн, figma и фирменный стиль.",
            "",
            False,
            "test source",
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertNotIn("✉️ Отклик (черновик)", message)
        self.assertNotIn("Здравствуйте!", message)

    def test_rss_fallback_message_mentions_incomplete_source(self):
        message, verdict = main.build_full_message(
            "Python API бот",
            "https://www.fl.ru/projects/789/api-bot/",
            "Нужен python api telegram бот для отчетов.",
            "RSS описание заказа",
            True,
            "RSS-лента fl.ru",
        )

        self.assertEqual(verdict, "✅ БРАТЬ")
        self.assertIn("Источник: RSS-лента fl.ru", message)
        self.assertIn("Анализ по RSS-ленте", message)


class ClaudeAnalyzeTests(unittest.TestCase):
    def test_missing_api_key_allows_order_without_external_call(self):
        with mock.patch.object(main.os, "getenv", return_value=""):
            self.assertTrue(main.claude_analyze("Title", "Description"))

    def test_no_answer_rejects_order(self):
        fake_client = SimpleNamespace(
            messages=SimpleNamespace(
                create=mock.Mock(
                    return_value=SimpleNamespace(
                        content=[SimpleNamespace(text="NO")]
                    )
                )
            )
        )
        fake_anthropic = SimpleNamespace(Anthropic=mock.Mock(return_value=fake_client))

        with mock.patch.object(main.os, "getenv", return_value="test-key"), \
                mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            self.assertFalse(main.claude_analyze("Logo", "Need design"))

        fake_anthropic.Anthropic.assert_called_once_with(api_key="test-key")
        fake_client.messages.create.assert_called_once()

    def test_api_exception_allows_order_to_avoid_dropping_jobs(self):
        fake_anthropic = SimpleNamespace(
            Anthropic=mock.Mock(side_effect=RuntimeError("network down"))
        )

        with mock.patch.object(main.os, "getenv", return_value="test-key"), \
                mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            self.assertTrue(main.claude_analyze("Python task", "Need backend"))


class MainLoopTests(unittest.TestCase):
    def test_claude_rejection_marks_processed_without_sending_notifications(self):
        rss_item = {
            "title": "Logo design",
            "link": "https://www.fl.ru/projects/901/logo-design/",
            "desc": "Need logo design",
        }
        conn = object()

        with mock.patch.object(main, "db_connect", return_value=conn), \
                mock.patch.object(main, "fetch_projects_page", return_value=[]), \
                mock.patch.object(main, "fetch_rss_items", return_value=[rss_item]), \
                mock.patch.object(main, "is_processed", return_value=False), \
                mock.patch.object(main, "http_get", return_value=("", 0)), \
                mock.patch.object(main, "claude_analyze", return_value=False), \
                mock.patch.object(main, "tg_send", return_value=True) as tg_send, \
                mock.patch.object(main, "email_send") as email_send, \
                mock.patch.object(main, "mark_processed") as mark_processed, \
                mock.patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        tg_send.assert_called_once()
        email_send.assert_not_called()
        mark_processed.assert_called_once_with(
            conn, "901", rss_item["link"], rss_item["title"]
        )

    def test_rss_fallback_uses_email_when_telegram_delivery_fails(self):
        rss_item = {
            "title": "Python API бот для отчетов",
            "link": "https://www.fl.ru/projects/902/python-api-bot/",
            "desc": "Нужен python api telegram бот для отчетов",
        }
        conn = object()

        with mock.patch.object(main, "db_connect", return_value=conn), \
                mock.patch.object(main, "fetch_projects_page", return_value=[]), \
                mock.patch.object(main, "fetch_rss_items", return_value=[rss_item]) as fetch_rss, \
                mock.patch.object(main, "is_processed", return_value=False), \
                mock.patch.object(main, "http_get", return_value=("", 0)), \
                mock.patch.object(main, "claude_analyze", return_value=True), \
                mock.patch.object(main, "tg_send", side_effect=[True, False]) as tg_send, \
                mock.patch.object(main, "email_send", return_value=True) as email_send, \
                mock.patch.object(main, "mark_processed") as mark_processed, \
                mock.patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        fetch_rss.assert_called_once_with()
        self.assertEqual(tg_send.call_count, 2)
        fallback_message = email_send.call_args.args[0]
        self.assertIn("Источник: RSS-лента fl.ru", fallback_message)
        self.assertIn("Анализ по RSS-ленте", fallback_message)
        mark_processed.assert_called_once_with(
            conn, "902", rss_item["link"], rss_item["title"]
        )


if __name__ == "__main__":
    unittest.main()
