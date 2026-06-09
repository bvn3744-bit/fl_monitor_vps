import importlib.util
import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


def load_main_module():
    module_path = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    spec = importlib.util.spec_from_file_location("fl_monitor_main", module_path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
        spec.loader.exec_module(module)
    return module


main = load_main_module()


class HttpGetTests(unittest.TestCase):
    def test_http_get_forces_utf8_even_when_apparent_encoding_differs(self):
        class FakeResponse:
            status_code = 200
            apparent_encoding = "windows-1251"

            def __init__(self):
                self.encoding = None
                self._body = "Привет".encode("utf-8")

            @property
            def text(self):
                return self._body.decode(self.encoding)

            def raise_for_status(self):
                return None

        response = FakeResponse()

        with mock.patch.object(main.time, "time", return_value=123), \
             mock.patch.object(main.requests, "get", return_value=response) as get_mock:
            text, status = main.http_get("https://example.test/path")

        self.assertEqual(text, "Привет")
        self.assertEqual(status, 200)
        self.assertEqual(response.encoding, "utf-8")
        self.assertEqual(get_mock.call_args.args[0], "https://example.test/path?_cb=123")


class MessageBuildTests(unittest.TestCase):
    def test_irrelevant_verdict_does_not_include_reply_draft(self):
        message, verdict = main.build_full_message(
            title="Нужен логотип и фирменный стиль",
            url="https://www.fl.ru/projects/123/",
            project_text="Нужен логотип, фирменный стиль, дизайн и smm продвижение.",
            rss_desc="",
            used_fallback=False,
            source_label="RSS-лента fl.ru",
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertNotIn("✉️ Отклик (черновик)", message)
        self.assertNotIn("Здравствуйте!", message)

    def test_relevant_verdict_includes_reply_draft(self):
        message, verdict = main.build_full_message(
            title="Python API интеграция",
            url="https://www.fl.ru/projects/456/",
            project_text="Нужен python backend скрипт для api интеграции и telegram бота.",
            rss_desc="",
            used_fallback=False,
            source_label="главная fl.ru/projects/",
        )

        self.assertEqual(verdict, "✅ БРАТЬ")
        self.assertIn("✉️ Отклик (черновик)", message)
        self.assertIn("Здравствуйте!", message)


class ClaudeAnalyzeTests(unittest.TestCase):
    def test_claude_no_response_rejects_project(self):
        fake_message = SimpleNamespace(
            content=[SimpleNamespace(text="NO")]
        )
        fake_client = SimpleNamespace(
            messages=SimpleNamespace(create=mock.Mock(return_value=fake_message))
        )
        fake_anthropic = SimpleNamespace(Anthropic=mock.Mock(return_value=fake_client))

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            result = main.claude_analyze("Дизайн логотипа", "Нужен логотип")

        self.assertFalse(result)
        fake_anthropic.Anthropic.assert_called_once_with(api_key="test-key")
        fake_client.messages.create.assert_called_once()


class MainLoopTests(unittest.TestCase):
    def test_claude_rejection_marks_project_without_sending_notification(self):
        class StopLoop(Exception):
            pass

        conn = object()
        project = {
            "title": "Настройка API",
            "link": "https://www.fl.ru/projects/789/test/",
            "desc": "Нужен backend API",
        }

        with mock.patch.object(main, "db_connect", return_value=conn), \
             mock.patch.object(main, "fetch_projects_page", return_value=[project]), \
             mock.patch.object(main, "fetch_rss_items") as fetch_rss_mock, \
             mock.patch.object(main, "title_is_anti", return_value=False), \
             mock.patch.object(main, "is_processed", return_value=False), \
             mock.patch.object(main, "http_get", return_value=("<main>Нужен backend API</main>", 200)), \
             mock.patch.object(main, "try_extract_project_text", return_value="Нужен backend API"), \
             mock.patch.object(main, "claude_analyze", return_value=False), \
             mock.patch.object(main, "build_full_message") as build_message_mock, \
             mock.patch.object(main, "tg_send", return_value=True) as tg_send_mock, \
             mock.patch.object(main, "email_send") as email_send_mock, \
             mock.patch.object(main, "mark_processed") as mark_processed_mock, \
             mock.patch.object(main.time, "sleep", side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                main.main()

        fetch_rss_mock.assert_not_called()
        build_message_mock.assert_not_called()
        email_send_mock.assert_not_called()
        self.assertEqual(tg_send_mock.call_count, 1, "only startup notification should be sent")
        mark_processed_mock.assert_called_once_with(conn, "789", project["link"], project["title"])


if __name__ == "__main__":
    unittest.main()
