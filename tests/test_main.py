import importlib.util
import os
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class StopLoop(BaseException):
    pass


def load_main_module():
    module_name = "fl_monitor_main_under_test"
    sys.modules.pop(module_name, None)

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = Mock()

    main_path = Path(__file__).resolve().parents[1] / "src" / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, main_path)
    module = importlib.util.module_from_spec(spec)

    with patch.dict(sys.modules, {"dotenv": fake_dotenv}), patch(
        "logging.FileHandler", side_effect=lambda *args, **kwargs: __import__("logging").NullHandler()
    ):
        spec.loader.exec_module(module)

    return module


def memory_db():
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


class MainImportTests(unittest.TestCase):
    def test_import_uses_dotenv_loader_without_reading_env_file(self):
        module = load_main_module()

        self.assertEqual("", module.TELEGRAM_BOT_TOKEN)
        self.assertEqual("", module.TELEGRAM_CHAT_ID)


class HttpGetTests(unittest.TestCase):
    def setUp(self):
        self.module = load_main_module()

    def test_http_get_forces_utf8_and_adds_cache_buster(self):
        class Response:
            status_code = 200
            apparent_encoding = "windows-1251"
            encoding = None

            def raise_for_status(self):
                return None

            @property
            def text(self):
                return self.encoding

        with patch.object(self.module.time, "time", return_value=123), patch.object(
            self.module.requests, "get", return_value=Response()
        ) as get:
            text, code = self.module.http_get("https://example.test/projects/?page=1")

        self.assertEqual(("utf-8", 200), (text, code))
        get.assert_called_once_with(
            "https://example.test/projects/?page=1&_cb=123",
            headers=self.module.HEADERS,
            timeout=self.module.HTTP_TIMEOUT,
        )

    def test_http_get_returns_immediately_on_forbidden(self):
        response = Mock(status_code=403)

        with patch.object(self.module.requests, "get", return_value=response) as get:
            text, code = self.module.http_get("https://example.test/projects/")

        self.assertEqual(("", 403), (text, code))
        self.assertEqual(1, get.call_count)
        response.raise_for_status.assert_not_called()


class MessageBuildTests(unittest.TestCase):
    def setUp(self):
        self.module = load_main_module()

    def test_non_relevant_message_does_not_include_reply_draft(self):
        message, verdict = self.module.build_full_message(
            "Логотип и фирменный стиль",
            "https://www.fl.ru/projects/100/design/",
            "Нужен логотип дизайн figma брендинг иллюстрации",
            "",
            False,
            "RSS-лента fl.ru",
        )

        self.assertEqual("❌ НЕ РЕЛЕВАНТНО", verdict)
        self.assertNotIn("✉️ Отклик (черновик)", message)
        self.assertNotIn("Могу взять в работу", message)

    def test_relevant_message_includes_reply_draft(self):
        message, verdict = self.module.build_full_message(
            "Python Telegram API бот",
            "https://www.fl.ru/projects/101/bot/",
            "Нужен python telegram api бот со sqlite и автоматизацией заявок",
            "",
            False,
            "главная fl.ru/projects/",
        )

        self.assertEqual("✅ БРАТЬ", verdict)
        self.assertIn("✉️ Отклик (черновик)", message)
        self.assertIn("Могу взять в работу", message)


class ClaudeAnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.module = load_main_module()

    def test_claude_analyze_allows_when_api_key_is_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(self.module.claude_analyze("Python parser", "Need parser"))

    def test_claude_analyze_rejects_explicit_no(self):
        content = types.SimpleNamespace(text="NO")
        message = types.SimpleNamespace(content=[content])
        messages = types.SimpleNamespace(create=Mock(return_value=message))
        client = types.SimpleNamespace(messages=messages)
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = Mock(return_value=client)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch.dict(
            sys.modules, {"anthropic": fake_anthropic}
        ):
            self.assertFalse(self.module.claude_analyze("Logo", "Need logo design"))

        fake_anthropic.Anthropic.assert_called_once_with(api_key="test-key")
        messages.create.assert_called_once()


class MainLoopTests(unittest.TestCase):
    def setUp(self):
        self.module = load_main_module()
        self.conn = memory_db()
        self.addCleanup(self.conn.close)

    def run_one_cycle(self):
        with patch.object(self.module.time, "sleep", side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                self.module.main()

    def test_main_falls_back_to_email_when_telegram_send_fails(self):
        item = {
            "title": "Python Telegram бот",
            "link": "https://www.fl.ru/projects/200/bot/",
            "desc": "Нужен python telegram api бот для автоматизации заявок",
        }

        with patch.object(self.module, "db_connect", return_value=self.conn), patch.object(
            self.module, "fetch_projects_page", return_value=[item]
        ), patch.object(self.module, "http_get", return_value=("", 0)), patch.object(
            self.module, "claude_analyze", return_value=True
        ), patch.object(
            self.module, "tg_send", side_effect=[True, False]
        ) as tg_send, patch.object(
            self.module, "email_send", return_value=True
        ) as email_send:
            self.run_one_cycle()

        self.assertEqual(2, tg_send.call_count)
        email_send.assert_called_once()
        self.assertTrue(self.module.is_processed(self.conn, "200"))

    def test_main_marks_claude_rejection_without_sending_notification(self):
        item = {
            "title": "Python parser",
            "link": "https://www.fl.ru/projects/201/parser/",
            "desc": "Нужен python parser для сайта",
        }

        with patch.object(self.module, "db_connect", return_value=self.conn), patch.object(
            self.module, "fetch_projects_page", return_value=[item]
        ), patch.object(self.module, "http_get", return_value=("", 0)), patch.object(
            self.module, "claude_analyze", return_value=False
        ), patch.object(
            self.module, "tg_send", return_value=True
        ) as tg_send, patch.object(
            self.module, "email_send", return_value=True
        ) as email_send:
            self.run_one_cycle()

        tg_send.assert_called_once()
        email_send.assert_not_called()
        self.assertTrue(self.module.is_processed(self.conn, "201"))


if __name__ == "__main__":
    unittest.main()
