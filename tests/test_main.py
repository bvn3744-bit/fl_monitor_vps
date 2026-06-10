import importlib.util
import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MAIN_PATH = os.path.join(ROOT_DIR, "src", "main.py")


def load_main_module():
    module_name = "fl_monitor_main_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch("logging.FileHandler", return_value=logging.NullHandler()):
        spec.loader.exec_module(module)
    return module


class HttpGetTests(unittest.TestCase):
    def test_http_get_forces_utf8_decoding(self):
        main = load_main_module()

        class Response:
            status_code = 200
            apparent_encoding = "windows-1251"
            encoding = None
            content = "Привет".encode("utf-8")

            @property
            def text(self):
                return self.content.decode(self.encoding)

            def raise_for_status(self):
                return None

        response = Response()

        with (
            mock.patch.object(main.time, "time", return_value=123456),
            mock.patch.object(main.time, "sleep") as sleep_mock,
            mock.patch.object(main.requests, "get", return_value=response) as get_mock,
        ):
            text, status = main.http_get("https://example.test/projects")

        self.assertEqual(text, "Привет")
        self.assertEqual(status, 200)
        self.assertEqual(response.encoding, "utf-8")
        sleep_mock.assert_not_called()
        get_mock.assert_called_once()
        requested_url = get_mock.call_args.args[0]
        self.assertEqual(requested_url, "https://example.test/projects?_cb=123456")


class MessageBuilderTests(unittest.TestCase):
    def test_build_full_message_omits_reply_draft_for_irrelevant_project(self):
        main = load_main_module()

        message, verdict = main.build_full_message(
            title="Нарисовать логотип и фирменный стиль",
            url="https://www.fl.ru/projects/10/",
            project_text="Нужно сделать логотип, фирменный стиль, дизайн и figma макеты",
            rss_desc="RSS fallback text",
            used_fallback=True,
            source_label="RSS-лента fl.ru",
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertIn("Анализ по RSS-ленте", message)
        self.assertNotIn("✉️ Отклик (черновик)", message)


class MainLoopTests(unittest.TestCase):
    def test_main_skips_title_anti_without_fetching_project_or_notifying(self):
        main = load_main_module()
        project = {
            "title": "Нужен маркетолог для продвижения канала",
            "link": "https://www.fl.ru/projects/101/",
            "desc": "irrelevant",
        }

        with (
            mock.patch.object(main, "db_connect", return_value=object()),
            mock.patch.object(main, "fetch_projects_page", return_value=[project]),
            mock.patch.object(main, "fetch_rss_items") as fetch_rss_mock,
            mock.patch.object(main, "http_get") as http_get_mock,
            mock.patch.object(main, "is_processed") as is_processed_mock,
            mock.patch.object(main, "claude_analyze") as claude_mock,
            mock.patch.object(main, "tg_send", return_value=True) as tg_mock,
            mock.patch.object(main, "email_send") as email_mock,
            mock.patch.object(main, "mark_processed") as mark_mock,
            mock.patch.object(main.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        tg_mock.assert_called_once()
        fetch_rss_mock.assert_not_called()
        http_get_mock.assert_not_called()
        is_processed_mock.assert_not_called()
        claude_mock.assert_not_called()
        email_mock.assert_not_called()
        mark_mock.assert_not_called()

    def test_main_marks_claude_rejected_project_without_delivery(self):
        main = load_main_module()
        conn = object()
        project = {
            "title": "Python парсер для сайта",
            "link": "https://www.fl.ru/projects/202/",
            "desc": "Сделать python парсер и API интеграцию",
        }

        with (
            mock.patch.object(main, "db_connect", return_value=conn),
            mock.patch.object(main, "fetch_projects_page", return_value=[project]),
            mock.patch.object(main, "is_processed", return_value=False),
            mock.patch.object(main, "http_get", return_value=("<html>project</html>", 200)),
            mock.patch.object(main, "try_extract_project_text", return_value="Python парсер с API интеграцией " * 8),
            mock.patch.object(main, "claude_analyze", return_value=False) as claude_mock,
            mock.patch.object(main, "build_full_message") as build_mock,
            mock.patch.object(main, "tg_send", return_value=True) as tg_mock,
            mock.patch.object(main, "email_send") as email_mock,
            mock.patch.object(main, "mark_processed") as mark_mock,
            mock.patch.object(main.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        claude_mock.assert_called_once_with(project["title"], "Python парсер с API интеграцией " * 8)
        mark_mock.assert_called_once_with(conn, "202", project["link"], project["title"])
        tg_mock.assert_called_once()
        build_mock.assert_not_called()
        email_mock.assert_not_called()

    def test_main_uses_email_fallback_when_telegram_delivery_fails(self):
        main = load_main_module()
        conn = object()
        project = {
            "title": "Python backend интеграция",
            "link": "https://www.fl.ru/projects/303/",
            "desc": "Нужна интеграция API",
        }

        with (
            mock.patch.object(main, "db_connect", return_value=conn),
            mock.patch.object(main, "fetch_projects_page", return_value=[project]),
            mock.patch.object(main, "is_processed", return_value=False),
            mock.patch.object(main, "http_get", return_value=("<html>project</html>", 200)),
            mock.patch.object(main, "try_extract_project_text", return_value="Python backend API интеграция " * 8),
            mock.patch.object(main, "claude_analyze", return_value=True),
            mock.patch.object(main, "build_full_message", return_value=("message body", "✅ БРАТЬ")),
            mock.patch.object(main, "tg_send", side_effect=[True, False]) as tg_mock,
            mock.patch.object(main, "email_send", return_value=True) as email_mock,
            mock.patch.object(main, "mark_processed") as mark_mock,
            mock.patch.object(main.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        self.assertEqual(tg_mock.call_count, 2)
        tg_mock.assert_any_call("message body")
        email_mock.assert_called_once_with("message body")
        mark_mock.assert_called_once_with(conn, "303", project["link"], project["title"])


if __name__ == "__main__":
    unittest.main()
