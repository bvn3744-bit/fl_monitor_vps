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
    """Import the monitor without reading local secrets or opening the log file."""
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
            "https://example.test/projects/?_cb=1234567890",
            calls[0][0],
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


class MainLoopNotificationTests(unittest.TestCase):
    class StopMonitor(BaseException):
        pass

    def test_telegram_failure_falls_back_to_email_and_marks_processed(self):
        conn = object()
        item = {
            "title": "Python API интеграция",
            "link": "https://www.fl.ru/projects/9001/example/",
            "desc": "RSS описание",
        }
        sent_to_telegram = []
        processed = []

        def fake_tg_send(text):
            sent_to_telegram.append(text)
            return len(sent_to_telegram) == 1

        def fake_mark_processed(conn_arg, pid, link, title):
            processed.append((conn_arg, pid, link, title))

        with mock.patch.object(app, "tg_send", side_effect=fake_tg_send):
            with mock.patch.object(app, "email_send") as email_send:
                with mock.patch.object(app, "db_connect", return_value=conn):
                    with mock.patch.object(app, "fetch_projects_page", return_value=[item]):
                        with mock.patch.object(app, "fetch_rss_items") as fetch_rss:
                            with mock.patch.object(app, "is_processed", return_value=False):
                                with mock.patch.object(app, "http_get", return_value=("<html></html>", 200)):
                                    with mock.patch.object(app, "try_extract_project_text", return_value="python api telegram бот " * 10):
                                        with mock.patch.object(app, "claude_analyze", return_value=True):
                                            with mock.patch.object(app, "build_full_message", return_value=("project-message", "✅ БРАТЬ")):
                                                with mock.patch.object(app, "mark_processed", side_effect=fake_mark_processed):
                                                    with mock.patch.object(app.time, "sleep", side_effect=self.StopMonitor):
                                                        with self.assertRaises(self.StopMonitor):
                                                            app.main()

        self.assertEqual(2, len(sent_to_telegram))
        self.assertEqual("project-message", sent_to_telegram[1])
        email_send.assert_called_once_with("project-message")
        fetch_rss.assert_not_called()
        self.assertEqual(
            [(conn, "9001", item["link"], item["title"])],
            processed,
        )

    def test_claude_rejection_marks_processed_without_notifications(self):
        conn = object()
        item = {
            "title": "Python API интеграция",
            "link": "https://www.fl.ru/projects/9002/example/",
            "desc": "RSS описание",
        }
        sent_to_telegram = []
        processed = []

        def fake_tg_send(text):
            sent_to_telegram.append(text)
            return True

        def fake_mark_processed(conn_arg, pid, link, title):
            processed.append((conn_arg, pid, link, title))

        with mock.patch.object(app, "tg_send", side_effect=fake_tg_send):
            with mock.patch.object(app, "email_send") as email_send:
                with mock.patch.object(app, "db_connect", return_value=conn):
                    with mock.patch.object(app, "fetch_projects_page", return_value=[item]):
                        with mock.patch.object(app, "fetch_rss_items") as fetch_rss:
                            with mock.patch.object(app, "is_processed", return_value=False):
                                with mock.patch.object(app, "http_get", return_value=("<html></html>", 200)):
                                    with mock.patch.object(app, "try_extract_project_text", return_value="python api telegram бот " * 10):
                                        with mock.patch.object(app, "claude_analyze", return_value=False):
                                            with mock.patch.object(app, "build_full_message") as build_full_message:
                                                with mock.patch.object(app, "mark_processed", side_effect=fake_mark_processed):
                                                    with mock.patch.object(app.time, "sleep", side_effect=self.StopMonitor):
                                                        with self.assertRaises(self.StopMonitor):
                                                            app.main()

        self.assertEqual(1, len(sent_to_telegram))
        email_send.assert_not_called()
        fetch_rss.assert_not_called()
        build_full_message.assert_not_called()
        self.assertEqual(
            [(conn, "9002", item["link"], item["title"])],
            processed,
        )


if __name__ == "__main__":
    unittest.main()
