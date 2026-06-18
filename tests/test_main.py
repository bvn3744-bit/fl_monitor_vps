import importlib.util
import logging
import os
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "main.py"

ENV_DEFAULTS = {
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "ANTHROPIC_API_KEY": "",
    "EMAIL_TO": "",
    "EMAIL_FROM": "",
    "EMAIL_PASSWORD": "",
    "EMAIL_SMTP": "",
    "INTERVAL_SECONDS": "20",
    "MAX_PER_CYCLE": "3",
}


def load_main_module():
    """Import src/main.py without reading .env or opening the log file."""
    module_name = "fl_monitor_main_for_tests"
    sys.modules.pop(module_name, None)

    fake_dotenv = types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)

    with (
        patch.dict(os.environ, ENV_DEFAULTS, clear=False),
        patch.dict(sys.modules, {"dotenv": fake_dotenv}),
        patch("logging.FileHandler", lambda *args, **kwargs: logging.NullHandler()),
    ):
        assert spec.loader is not None
        spec.loader.exec_module(module)

    return module


def processed_connection():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE processed (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    return conn


class HttpGetTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_http_get_forces_utf8_and_adds_query_cache_buster(self):
        response = Mock(status_code=200, text="ok")
        response.raise_for_status = Mock()

        with (
            patch.object(self.main.time, "time", return_value=1234567890),
            patch.object(self.main.requests, "get", return_value=response) as get,
        ):
            text, status = self.main.http_get("https://example.test/projects/?page=1")

        self.assertEqual(("ok", 200), (text, status))
        self.assertEqual("utf-8", response.encoding)
        response.raise_for_status.assert_called_once_with()
        get.assert_called_once_with(
            "https://example.test/projects/?page=1&_cb=1234567890",
            headers=self.main.HEADERS,
            timeout=self.main.HTTP_TIMEOUT,
        )

    def test_http_get_returns_403_without_retrying_or_sleeping(self):
        response = Mock(status_code=403, text="forbidden")

        with (
            patch.object(self.main.requests, "get", return_value=response) as get,
            patch.object(self.main.time, "sleep") as sleep,
        ):
            text, status = self.main.http_get("https://example.test/projects/")

        self.assertEqual(("", 403), (text, status))
        self.assertEqual(1, get.call_count)
        sleep.assert_not_called()
        response.raise_for_status.assert_not_called()


class MessageBuildTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_build_full_message_includes_reply_draft_only_for_relevant_verdicts(self):
        relevant_text = (
            "Нужен Python backend разработчик: FastAPI, API интеграция, "
            "telegram бот, sqlite и автоматизация процессов."
        )
        relevant_message, relevant_verdict = self.main.build_full_message(
            "Backend automation",
            "https://www.fl.ru/projects/101/backend/",
            relevant_text,
            "",
            False,
            "test source",
        )

        rejected_text = "Нужен логотип, фирменный стиль, дизайн, figma и ui ux макеты."
        rejected_message, rejected_verdict = self.main.build_full_message(
            "Brand design",
            "https://www.fl.ru/projects/102/design/",
            rejected_text,
            "",
            False,
            "test source",
        )

        self.assertIn("✉️ Отклик (черновик)", relevant_message)
        self.assertIn(relevant_verdict, ("✅ БРАТЬ", "🟡 МОЖНО СМОТРЕТЬ"))
        self.assertNotIn("✉️ Отклик (черновик)", rejected_message)
        self.assertEqual("❌ НЕ РЕЛЕВАНТНО", rejected_verdict)


class ClaudeAnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_claude_analyze_allows_project_when_api_key_is_missing(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            self.assertTrue(self.main.claude_analyze("Any title", "Any description"))

    def test_claude_analyze_rejects_explicit_no_response(self):
        create = Mock(
            return_value=types.SimpleNamespace(
                content=[types.SimpleNamespace(text="NO")]
            )
        )
        fake_client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=create)
        )
        fake_anthropic = types.SimpleNamespace(
            Anthropic=Mock(return_value=fake_client)
        )

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False),
            patch.dict(sys.modules, {"anthropic": fake_anthropic}),
        ):
            result = self.main.claude_analyze("Logo task", "Only logo design")

        self.assertFalse(result)
        fake_anthropic.Anthropic.assert_called_once_with(api_key="test-key")
        create.assert_called_once()
        _, kwargs = create.call_args
        self.assertEqual("claude-haiku-4-5-20251001", kwargs["model"])
        self.assertIn("Logo task", kwargs["messages"][0]["content"])


class MainLoopTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_main_marks_claude_rejected_project_without_customer_notification(self):
        conn = processed_connection()
        item = {
            "title": "Backend automation",
            "link": "https://www.fl.ru/projects/777/backend-automation/",
            "desc": "RSS fallback description",
        }

        with (
            patch.object(self.main, "db_connect", return_value=conn),
            patch.object(self.main, "fetch_projects_page", return_value=[item]),
            patch.object(self.main, "fetch_rss_items") as fetch_rss_items,
            patch.object(self.main, "http_get", return_value=("<html>project</html>", 200)),
            patch.object(self.main, "try_extract_project_text", return_value="Python API backend task"),
            patch.object(self.main, "claude_analyze", return_value=False) as claude_analyze,
            patch.object(self.main, "build_full_message") as build_full_message,
            patch.object(self.main, "tg_send", return_value=True) as tg_send,
            patch.object(self.main, "email_send") as email_send,
            patch.object(self.main.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.main.main()

        claude_analyze.assert_called_once_with("Backend automation", "Python API backend task")
        build_full_message.assert_not_called()
        fetch_rss_items.assert_not_called()
        self.assertEqual(1, tg_send.call_count, "only startup notification should be sent")
        email_send.assert_not_called()
        self.assertTrue(self.main.is_processed(conn, "777"))


if __name__ == "__main__":
    unittest.main()
