import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "main.py"

spec = importlib.util.spec_from_file_location("fl_monitor_main", MODULE_PATH)
app = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(app)


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
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
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

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
                self.assertFalse(app.claude_analyze("Логотип", "Нужен дизайн"))

        self.assertEqual("claude-haiku-4-5-20251001", created["model"])
        self.assertEqual(10, created["max_tokens"])
        self.assertIn("Ответь строго одним словом", created["messages"][0]["content"])


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
            "https://example.test/projects/?_cb=1234567890", calls[0][0]
        )
        self.assertIs(calls[0][1], app.HEADERS)
        self.assertEqual(app.HTTP_TIMEOUT, calls[0][2])


class EmailSendTests(unittest.TestCase):
    def test_missing_email_config_returns_false_without_smtp(self):
        empty_email_env = {
            "EMAIL_TO": "",
            "EMAIL_FROM": "",
            "EMAIL_PASSWORD": "",
            "EMAIL_SMTP": "",
            "EMAIL_PORT": "465",
        }

        with mock.patch.dict(os.environ, empty_email_env):
            with mock.patch.object(app.smtplib, "SMTP_SSL") as smtp_ssl:
                self.assertFalse(app.email_send("body"))

        smtp_ssl.assert_not_called()

    def test_port_587_uses_starttls_before_login_and_send(self):
        events = []

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                events.append(("connect", host, port, timeout))

            def __enter__(self):
                events.append(("enter",))
                return self

            def __exit__(self, exc_type, exc, tb):
                events.append(("exit", exc_type))

            def starttls(self):
                events.append(("starttls",))

            def login(self, user, password):
                events.append(("login", user, password))

            def sendmail(self, from_addr, to_addr, message):
                events.append(("sendmail", from_addr, to_addr, message))

        email_env = {
            "EMAIL_TO": "client@example.test",
            "EMAIL_FROM": "monitor@example.test",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_SMTP": "smtp.example.test",
            "EMAIL_PORT": "587",
        }

        with mock.patch.dict(os.environ, email_env):
            with mock.patch.object(app.smtplib, "SMTP", FakeSMTP):
                with mock.patch.object(app.smtplib, "SMTP_SSL") as smtp_ssl:
                    self.assertTrue(app.email_send("Тестовое сообщение"))

        self.assertEqual(
            [
                ("connect", "smtp.example.test", 587, 30),
                ("enter",),
                ("starttls",),
                ("login", "monitor@example.test", "secret"),
            ],
            events[:4],
        )
        self.assertEqual("sendmail", events[4][0])
        self.assertEqual("monitor@example.test", events[4][1])
        self.assertEqual("client@example.test", events[4][2])
        self.assertIn(b"FL Monitor", events[4][3])
        self.assertEqual(("exit", None), events[5])
        smtp_ssl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
