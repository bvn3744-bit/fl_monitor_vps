# -*- coding: utf-8 -*-

import importlib.util
import logging
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "src" / "main.py"
MODULE_NAME = "fl_monitor_main_under_test"


def load_main_module():
    sys.modules.pop(MODULE_NAME, None)

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *_args, **_kwargs: None

    env_overrides = {
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "ANTHROPIC_API_KEY": "",
    }

    with mock.patch.dict(sys.modules, {"dotenv": fake_dotenv}):
        with mock.patch.dict(os.environ, env_overrides, clear=False):
            with mock.patch.object(
                logging,
                "FileHandler",
                side_effect=lambda *_args, **_kwargs: logging.NullHandler(),
            ):
                spec = importlib.util.spec_from_file_location(MODULE_NAME, MAIN_PATH)
                module = importlib.util.module_from_spec(spec)
                sys.modules[MODULE_NAME] = module
                spec.loader.exec_module(module)
                return module


class FakeResponse:
    def __init__(self, status_code=200, text="ok", apparent_encoding="windows-1251"):
        self.status_code = status_code
        self.text = text
        self.apparent_encoding = apparent_encoding
        self.encoding = None
        self.raise_for_status_called = False

    def raise_for_status(self):
        self.raise_for_status_called = True


class FlMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main_module()

    def test_http_get_adds_cache_buster_and_forces_utf8(self):
        response = FakeResponse(text="привет")

        with mock.patch.object(self.main.time, "time", return_value=12345):
            with mock.patch.object(self.main.requests, "get", return_value=response) as get:
                body, status = self.main.http_get("https://example.test/path?x=1")

        self.assertEqual(body, "привет")
        self.assertEqual(status, 200)
        self.assertEqual(response.encoding, "utf-8")
        self.assertTrue(response.raise_for_status_called)
        get.assert_called_once()
        requested_url = get.call_args.args[0]
        self.assertEqual(requested_url, "https://example.test/path?x=1&_cb=12345")

    def test_http_get_returns_403_without_retry_or_sleep(self):
        response = FakeResponse(status_code=403, text="forbidden")

        with mock.patch.object(self.main.requests, "get", return_value=response) as get:
            with mock.patch.object(self.main.time, "sleep") as sleep:
                body, status = self.main.http_get("https://example.test/projects/")

        self.assertEqual((body, status), ("", 403))
        get.assert_called_once()
        sleep.assert_not_called()
        self.assertFalse(response.raise_for_status_called)

    def test_build_full_message_only_includes_reply_draft_for_relevant_verdicts(self):
        irrelevant_text = "логотип дизайн figma"
        irrelevant_msg, irrelevant_verdict = self.main.build_full_message(
            "Логотип для сайта",
            "https://www.fl.ru/projects/1/logo/",
            irrelevant_text,
            "RSS описание",
            True,
            "RSS-лента fl.ru",
        )

        self.assertEqual(irrelevant_verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertIn("Анализ по RSS-ленте", irrelevant_msg)
        self.assertNotIn("✉️ Отклик (черновик)", irrelevant_msg)

        relevant_text = (
            "Нужен python backend бот с API, SQL, FastAPI и telegram интеграцией. "
            "Автоматизация должна запускаться на Linux VPS."
        )
        relevant_msg, relevant_verdict = self.main.build_full_message(
            "Python API бот",
            "https://www.fl.ru/projects/2/bot/",
            relevant_text,
            "",
            False,
            "главная fl.ru/projects/",
        )

        self.assertEqual(relevant_verdict, "✅ БРАТЬ")
        self.assertIn("✉️ Отклик (черновик)", relevant_msg)

    def test_claude_analyze_fails_open_without_api_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            self.assertTrue(self.main.claude_analyze("Python bot", "Need backend automation"))

    def test_claude_analyze_rejects_explicit_no_response(self):
        messages = mock.Mock()
        messages.create.return_value = types.SimpleNamespace(
            content=[types.SimpleNamespace(text="NO")]
        )
        client = types.SimpleNamespace(messages=messages)
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = mock.Mock(return_value=client)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
                self.assertFalse(self.main.claude_analyze("Logo", "Need design only"))

        fake_anthropic.Anthropic.assert_called_once_with(api_key="test-key")
        messages.create.assert_called_once()
        prompt = messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Logo", prompt)
        self.assertIn("Need design only", prompt)

    def test_main_marks_claude_rejection_processed_without_notification(self):
        item = {
            "title": "Python API automation",
            "link": "https://www.fl.ru/projects/100/python-api/",
            "desc": "RSS description",
        }
        long_project_text = "Python backend automation with API and SQL. " * 5

        with mock.patch.object(self.main, "db_connect", return_value=object()):
            with mock.patch.object(self.main, "fetch_projects_page", return_value=[item]):
                with mock.patch.object(self.main, "fetch_rss_items") as fetch_rss:
                    with mock.patch.object(self.main, "is_processed", return_value=False):
                        with mock.patch.object(self.main, "http_get", return_value=("<html></html>", 200)):
                            with mock.patch.object(self.main, "try_extract_project_text", return_value=long_project_text):
                                with mock.patch.object(self.main, "claude_analyze", return_value=False):
                                    with mock.patch.object(self.main, "build_full_message") as build_message:
                                        with mock.patch.object(self.main, "mark_processed") as mark_processed:
                                            with mock.patch.object(self.main, "email_send") as email_send:
                                                with mock.patch.object(self.main, "tg_send", return_value=True) as tg_send:
                                                    with mock.patch.object(
                                                        self.main.time,
                                                        "sleep",
                                                        side_effect=KeyboardInterrupt,
                                                    ):
                                                        with self.assertRaises(KeyboardInterrupt):
                                                            self.main.main()

        fetch_rss.assert_not_called()
        mark_processed.assert_called_once_with(
            mock.ANY,
            "100",
            "https://www.fl.ru/projects/100/python-api/",
            "Python API automation",
        )
        build_message.assert_not_called()
        email_send.assert_not_called()
        tg_send.assert_called_once()

    def test_main_uses_email_fallback_when_telegram_delivery_fails(self):
        item = {
            "title": "Python API automation",
            "link": "https://www.fl.ru/projects/101/python-api/",
            "desc": "RSS description",
        }
        long_project_text = "Python backend automation with API and SQL. " * 5

        with mock.patch.object(self.main, "db_connect", return_value=object()):
            with mock.patch.object(self.main, "fetch_projects_page", return_value=[item]):
                with mock.patch.object(self.main, "is_processed", return_value=False):
                    with mock.patch.object(self.main, "http_get", return_value=("<html></html>", 200)):
                        with mock.patch.object(self.main, "try_extract_project_text", return_value=long_project_text):
                            with mock.patch.object(self.main, "claude_analyze", return_value=True):
                                with mock.patch.object(
                                    self.main,
                                    "build_full_message",
                                    return_value=("message", "✅ БРАТЬ"),
                                ):
                                    with mock.patch.object(self.main, "mark_processed") as mark_processed:
                                        with mock.patch.object(self.main, "email_send", return_value=True) as email_send:
                                            with mock.patch.object(
                                                self.main,
                                                "tg_send",
                                                side_effect=[True, False],
                                            ) as tg_send:
                                                with mock.patch.object(
                                                    self.main.time,
                                                    "sleep",
                                                    side_effect=KeyboardInterrupt,
                                                ):
                                                    with self.assertRaises(KeyboardInterrupt):
                                                        self.main.main()

        self.assertEqual(tg_send.call_count, 2)
        email_send.assert_called_once_with("message")
        mark_processed.assert_called_once_with(
            mock.ANY,
            "101",
            "https://www.fl.ru/projects/101/python-api/",
            "Python API automation",
        )


if __name__ == "__main__":
    unittest.main()
