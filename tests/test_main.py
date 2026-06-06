import importlib.util
import logging
import sys
import types
from email import message_from_bytes
from email.header import decode_header, make_header
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "src" / "main.py"


def load_main_module():
    spec = importlib.util.spec_from_file_location("fl_monitor_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.object(logging, "FileHandler", return_value=logging.NullHandler()):
        spec.loader.exec_module(module)
    return module


main = load_main_module()


class FakeResponse:
    def __init__(self, status_code=200, body="", apparent_encoding="utf-8"):
        self.status_code = status_code
        self.content = body.encode("utf-8")
        self.apparent_encoding = apparent_encoding
        self.encoding = None

    @property
    def text(self):
        return self.content.decode(self.encoding or self.apparent_encoding)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class HttpTests(unittest.TestCase):
    def test_http_get_forces_utf8_before_reading_body(self):
        response = FakeResponse(body="Привет, API", apparent_encoding="latin-1")

        with patch.object(main.time, "time", return_value=1000), \
                patch.object(main.requests, "get", return_value=response) as request_get:
            text, code = main.http_get("https://example.test/projects")

        self.assertEqual(text, "Привет, API")
        self.assertEqual(code, 200)
        self.assertEqual(response.encoding, "utf-8")
        request_get.assert_called_once()
        self.assertEqual(request_get.call_args.args[0], "https://example.test/projects?_cb=1000")

    def test_http_get_returns_403_without_retrying_or_sleeping(self):
        with patch.object(main.requests, "get", return_value=FakeResponse(status_code=403)) as request_get, \
                patch.object(main.time, "sleep") as sleep:
            text, code = main.http_get("https://example.test/blocked")

        self.assertEqual((text, code), ("", 403))
        request_get.assert_called_once()
        sleep.assert_not_called()


class MessageTests(unittest.TestCase):
    def test_build_full_message_only_includes_reply_draft_for_relevant_projects(self):
        irrelevant_message, irrelevant_verdict = main.build_full_message(
            "Логотип и дизайн",
            "https://www.fl.ru/projects/10/design/",
            "Нужно сделать логотип фирменный стиль дизайн figma",
            "",
            False,
            "test",
        )
        relevant_message, relevant_verdict = main.build_full_message(
            "Python API бот",
            "https://www.fl.ru/projects/11/python-api-bot/",
            "Нужен python api telegram бот для автоматизации отчетов",
            "",
            False,
            "test",
        )

        self.assertEqual(irrelevant_verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertNotIn("✉️ Отклик (черновик)", irrelevant_message)
        self.assertEqual(relevant_verdict, "✅ БРАТЬ")
        self.assertIn("✉️ Отклик (черновик)", relevant_message)


class EmailTests(unittest.TestCase):
    def test_email_send_requires_complete_configuration(self):
        with patch.dict(main.os.environ, {}, clear=True), \
                patch.object(main.smtplib, "SMTP_SSL") as smtp_ssl:
            ok = main.email_send("message")

        self.assertFalse(ok)
        smtp_ssl.assert_not_called()

    def test_email_send_uses_smtp_ssl_by_default(self):
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=None)

        env = {
            "EMAIL_TO": "to@example.test",
            "EMAIL_FROM": "from@example.test",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_SMTP": "smtp.example.test",
        }
        with patch.dict(main.os.environ, env, clear=True), \
                patch.object(main.smtplib, "SMTP_SSL", return_value=server) as smtp_ssl:
            ok = main.email_send("Текст сообщения")

        self.assertTrue(ok)
        smtp_ssl.assert_called_once_with("smtp.example.test", 465, timeout=30)
        server.login.assert_called_once_with("from@example.test", "secret")
        server.sendmail.assert_called_once()
        sent_message = server.sendmail.call_args.args[2]
        parsed_message = message_from_bytes(sent_message)
        subject = str(make_header(decode_header(parsed_message["Subject"])))
        self.assertEqual(subject, "FL Monitor: новый заказ")
        self.assertEqual(parsed_message["From"], "from@example.test")
        self.assertEqual(parsed_message["To"], "to@example.test")

    def test_email_send_uses_starttls_for_submission_port(self):
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=None)

        env = {
            "EMAIL_TO": "to@example.test",
            "EMAIL_FROM": "from@example.test",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_SMTP": "smtp.example.test",
            "EMAIL_PORT": "587",
        }
        with patch.dict(main.os.environ, env, clear=True), \
                patch.object(main.smtplib, "SMTP", return_value=server) as smtp:
            ok = main.email_send("message")

        self.assertTrue(ok)
        smtp.assert_called_once_with("smtp.example.test", 587, timeout=30)
        server.starttls.assert_called_once_with()
        server.login.assert_called_once_with("from@example.test", "secret")
        server.sendmail.assert_called_once()


class ClaudeAnalyzeTests(unittest.TestCase):
    def test_claude_analyze_allows_when_api_key_is_absent(self):
        with patch.dict(main.os.environ, {}, clear=True):
            self.assertTrue(main.claude_analyze("Python бот", "Нужен бот"))

    def test_claude_analyze_rejects_explicit_no_response(self):
        class FakeAnthropicClient:
            def __init__(self, api_key):
                self.api_key = api_key
                self.messages = Mock()
                self.messages.create.return_value = types.SimpleNamespace(
                    content=[types.SimpleNamespace(text="NO")]
                )

        fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)

        with patch.dict(main.os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True), \
                patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            result = main.claude_analyze("Логотип", "Нужен дизайн логотипа")

        self.assertFalse(result)

    def test_claude_analyze_fails_open_on_api_error(self):
        class FakeAnthropicClient:
            def __init__(self, api_key):
                self.messages = Mock()
                self.messages.create.side_effect = RuntimeError("api down")

        fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)

        with patch.dict(main.os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True), \
                patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            result = main.claude_analyze("Python бот", "Нужен бот")

        self.assertTrue(result)


class MainLoopTests(unittest.TestCase):
    def test_main_marks_claude_rejected_project_without_sending(self):
        item = {
            "title": "Логотип для сайта",
            "link": "https://www.fl.ru/projects/901/logo/",
            "desc": "Нужен логотип и фирменный стиль",
        }
        conn = object()

        with patch.object(main, "db_connect", return_value=conn), \
                patch.object(main, "fetch_projects_page", return_value=[item]), \
                patch.object(main, "fetch_rss_items") as fetch_rss, \
                patch.object(main, "title_is_anti", return_value=False), \
                patch.object(main, "is_processed", return_value=False), \
                patch.object(main, "http_get", return_value=("", 0)), \
                patch.object(main, "claude_analyze", return_value=False) as claude_analyze, \
                patch.object(main, "tg_send", return_value=True) as tg_send, \
                patch.object(main, "email_send") as email_send, \
                patch.object(main, "mark_processed") as mark_processed, \
                patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        fetch_rss.assert_not_called()
        claude_analyze.assert_called_once_with(item["title"], item["desc"])
        tg_send.assert_called_once()
        email_send.assert_not_called()
        mark_processed.assert_called_once_with(conn, "901", item["link"], item["title"])

    def test_main_uses_rss_fallback_and_email_when_telegram_delivery_fails(self):
        rss_item = {
            "title": "Python API бот для отчетов",
            "link": "https://www.fl.ru/projects/902/python-api-bot/",
            "desc": "Нужен python api telegram бот для отчетов",
        }
        conn = object()

        with patch.object(main, "db_connect", return_value=conn), \
                patch.object(main, "fetch_projects_page", return_value=[]), \
                patch.object(main, "fetch_rss_items", return_value=[rss_item]) as fetch_rss, \
                patch.object(main, "is_processed", return_value=False), \
                patch.object(main, "http_get", return_value=("", 0)), \
                patch.object(main, "claude_analyze", return_value=True), \
                patch.object(main, "tg_send", side_effect=[True, False]) as tg_send, \
                patch.object(main, "email_send", return_value=True) as email_send, \
                patch.object(main, "mark_processed") as mark_processed, \
                patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.main()

        fetch_rss.assert_called_once_with()
        self.assertEqual(tg_send.call_count, 2)
        fallback_message = email_send.call_args.args[0]
        self.assertIn("Источник: RSS-лента fl.ru", fallback_message)
        self.assertIn("Анализ по RSS-ленте", fallback_message)
        mark_processed.assert_called_once_with(conn, "902", rss_item["link"], rss_item["title"])


if __name__ == "__main__":
    unittest.main()
