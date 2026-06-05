import importlib.util
import logging
import unittest
from pathlib import Path
from unittest.mock import patch


def load_main_module():
    module_path = Path(__file__).resolve().parents[1] / "src" / "main.py"
    spec = importlib.util.spec_from_file_location("fl_monitor_main_for_tests", module_path)
    module = importlib.util.module_from_spec(spec)
    with patch("logging.FileHandler", side_effect=lambda *args, **kwargs: logging.NullHandler()):
        spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code=200, content=b"", apparent_encoding="windows-1251"):
        self.status_code = status_code
        self.content = content
        self.apparent_encoding = apparent_encoding
        self.encoding = None

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class HttpGetTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_http_get_decodes_utf8_even_when_apparent_encoding_is_wrong(self):
        response = FakeResponse(
            content="Новый заказ: телеграм бот".encode("utf-8"),
            apparent_encoding="windows-1251",
        )

        with (
            patch.object(self.main.time, "time", return_value=1234567890),
            patch.object(self.main.time, "sleep") as sleep,
            patch.object(self.main.requests, "get", return_value=response) as requests_get,
        ):
            text, code = self.main.http_get("https://example.test/projects/")

        self.assertEqual(code, 200)
        self.assertEqual(text, "Новый заказ: телеграм бот")
        self.assertEqual(response.encoding, "utf-8")
        requests_get.assert_called_once()
        requested_url = requests_get.call_args.args[0]
        self.assertEqual(requested_url, "https://example.test/projects/?_cb=1234567890")
        sleep.assert_not_called()

    def test_http_get_returns_403_without_retrying_or_reading_body(self):
        response = FakeResponse(status_code=403, content="Forbidden".encode("utf-8"))

        with (
            patch.object(self.main.time, "sleep") as sleep,
            patch.object(self.main.requests, "get", return_value=response) as requests_get,
        ):
            text, code = self.main.http_get("https://example.test/projects/")

        self.assertEqual((text, code), ("", 403))
        requests_get.assert_called_once()
        sleep.assert_not_called()


class BuildFullMessageTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_reply_draft_is_omitted_for_irrelevant_projects(self):
        project_text = (
            "Нужен логотип, фирменный стиль, дизайн и figma макеты для бренда. "
            "Также нужны баннеры и иллюстрации. "
        ) * 6

        message, verdict = self.main.build_full_message(
            "Логотип и фирменный стиль",
            "https://www.fl.ru/projects/100/",
            project_text,
            "",
            False,
            "главная fl.ru/projects/",
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertNotIn("✉️ Отклик (черновик)", message)
        self.assertNotIn("Здравствуйте!", message)

    def test_reply_draft_is_included_for_relevant_projects(self):
        project_text = (
            "Нужен Python backend бот для Telegram API, автоматизация и интеграция "
            "на сервере Ubuntu с базой SQL. "
        ) * 6

        message, verdict = self.main.build_full_message(
            "Python Telegram API бот",
            "https://www.fl.ru/projects/101/",
            project_text,
            "",
            False,
            "главная fl.ru/projects/",
        )

        self.assertEqual(verdict, "✅ БРАТЬ")
        self.assertIn("✉️ Отклик (черновик)", message)
        self.assertIn("Здравствуйте!", message)


if __name__ == "__main__":
    unittest.main()
