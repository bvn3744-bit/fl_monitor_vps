import importlib.util
import os
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "src" / "main.py"


def load_monitor():
    spec = importlib.util.spec_from_file_location("monitor_under_test", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with patch("logging.FileHandler", return_value=__import__("logging").NullHandler()):
        spec.loader.exec_module(module)
    return module


def memory_db(module):
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


class FakeResponse:
    def __init__(self, status_code=200, body=b"", apparent_encoding=None):
        self.status_code = status_code
        self._body = body
        self.apparent_encoding = apparent_encoding
        self.text_for_warning = ""

    @property
    def text(self):
        encoding = getattr(self, "encoding", None) or self.apparent_encoding or "utf-8"
        return self._body.decode(encoding)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class HttpGetTests(unittest.TestCase):
    def test_http_get_forces_utf8_even_when_apparent_encoding_is_wrong(self):
        module = load_monitor()
        response = FakeResponse(
            status_code=200,
            body="Новый заказ".encode("utf-8"),
            apparent_encoding="cp1251",
        )

        with (
            patch.object(module.time, "time", return_value=123456),
            patch.object(module.requests, "get", return_value=response) as get,
        ):
            text, code = module.http_get("https://www.fl.ru/projects/?kind=1")

        self.assertEqual("Новый заказ", text)
        self.assertEqual(200, code)
        get.assert_called_once()
        called_url = get.call_args.args[0]
        self.assertEqual("https://www.fl.ru/projects/?kind=1&_cb=123456", called_url)
        self.assertEqual("utf-8", response.encoding)

    def test_http_get_returns_403_without_retry_delay(self):
        module = load_monitor()

        with (
            patch.object(module.requests, "get", return_value=FakeResponse(status_code=403)) as get,
            patch.object(module.time, "sleep") as sleep,
        ):
            text, code = module.http_get("https://www.fl.ru/projects/")

        self.assertEqual("", text)
        self.assertEqual(403, code)
        get.assert_called_once()
        sleep.assert_not_called()


class MessageAndClaudeTests(unittest.TestCase):
    def test_build_full_message_omits_reply_draft_for_irrelevant_project(self):
        module = load_monitor()
        text = "Нужен дизайн логотипа, фирменный стиль, figma и баннеры для рекламы."

        message, verdict = module.build_full_message(
            "Дизайн логотипа",
            "https://www.fl.ru/projects/100/design/",
            text,
            "",
            used_fallback=False,
            source_label="RSS-лента fl.ru",
        )

        self.assertEqual("❌ НЕ РЕЛЕВАНТНО", verdict)
        self.assertNotIn("✉️ Отклик (черновик)", message)
        self.assertNotIn("Готов начать сразу", message)

    def test_build_full_message_includes_reply_draft_for_relevant_project(self):
        module = load_monitor()
        text = (
            "Нужен Python backend скрипт для интеграции API и Telegram бота. "
            "Есть VPS, SQLite база и понятное техническое задание."
        )

        message, verdict = module.build_full_message(
            "Python API бот",
            "https://www.fl.ru/projects/101/python/",
            text,
            "",
            used_fallback=False,
            source_label="главная fl.ru/projects/",
        )

        self.assertEqual("✅ БРАТЬ", verdict)
        self.assertIn("✉️ Отклик (черновик)", message)
        self.assertIn("Готов начать сразу", message)

    def test_claude_analyze_rejects_explicit_no_response(self):
        module = load_monitor()

        class FakeMessages:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text="NO - design work")]
                )

        fake_messages = FakeMessages()

        class FakeAnthropic:
            def __init__(self, api_key):
                self.api_key = api_key
                self.messages = fake_messages

        fake_anthropic_module = types.SimpleNamespace(Anthropic=FakeAnthropic)

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch.dict(sys.modules, {"anthropic": fake_anthropic_module}),
        ):
            result = module.claude_analyze("Логотип", "Нужен дизайн логотипа")

        self.assertFalse(result)
        self.assertEqual("claude-haiku-4-5-20251001", fake_messages.kwargs["model"])
        self.assertEqual(10, fake_messages.kwargs["max_tokens"])
        self.assertIn("Ответь строго одним словом", fake_messages.kwargs["messages"][0]["content"])


class MainLoopTests(unittest.TestCase):
    def test_main_marks_claude_rejected_project_without_sending_notification(self):
        module = load_monitor()
        conn = memory_db(module)
        item = {
            "title": "Python API задача",
            "link": "https://www.fl.ru/projects/777/python-api/",
            "desc": "Нужно сделать Python API интеграцию",
        }

        with (
            patch.object(module, "db_connect", return_value=conn),
            patch.object(module, "fetch_projects_page", return_value=[item]),
            patch.object(module, "fetch_rss_items") as fetch_rss,
            patch.object(module, "http_get", return_value=("<html>details</html>", 200)),
            patch.object(module, "try_extract_project_text", return_value="Python API integration details"),
            patch.object(module, "claude_analyze", return_value=False),
            patch.object(module, "tg_send", return_value=True) as tg_send,
            patch.object(module, "email_send") as email_send,
            patch.object(module.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                module.main()

        self.assertTrue(module.is_processed(conn, "777"))
        self.assertEqual(1, tg_send.call_count, "only startup notification should be sent")
        fetch_rss.assert_not_called()
        email_send.assert_not_called()

    def test_main_falls_back_to_email_when_telegram_project_send_fails(self):
        module = load_monitor()
        conn = memory_db(module)
        item = {
            "title": "Python Telegram бот",
            "link": "https://www.fl.ru/projects/888/python-telegram/",
            "desc": "Нужен Python Telegram бот",
        }

        with (
            patch.object(module, "db_connect", return_value=conn),
            patch.object(module, "fetch_projects_page", return_value=[item]),
            patch.object(module, "fetch_rss_items"),
            patch.object(module, "http_get", return_value=("<html>details</html>", 200)),
            patch.object(
                module,
                "try_extract_project_text",
                return_value="Python API Telegram bot integration with SQLite backend",
            ),
            patch.object(module, "claude_analyze", return_value=True),
            patch.object(module, "tg_send", side_effect=[True, False]) as tg_send,
            patch.object(module, "email_send", return_value=True) as email_send,
            patch.object(module.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                module.main()

        self.assertEqual(2, tg_send.call_count, "startup and project notifications should be attempted")
        email_send.assert_called_once()
        self.assertIn("Python Telegram бот", email_send.call_args.args[0])
        self.assertTrue(module.is_processed(conn, "888"))


if __name__ == "__main__":
    unittest.main()
