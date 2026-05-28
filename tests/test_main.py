# -*- coding: utf-8 -*-
import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def load_main_module():
    module_path = Path(__file__).resolve().parents[1] / "src" / "main.py"
    spec = importlib.util.spec_from_file_location("fl_monitor_main", module_path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(logging, "FileHandler", return_value=logging.NullHandler()):
        spec.loader.exec_module(module)
    return module


main = load_main_module()


class KeywordMatchingTests(unittest.TestCase):
    def test_whole_word_keywords_do_not_match_inside_larger_words(self):
        text = "Нужен premium лендинг с fluid layout и prefix API."

        hits = main.count_matches(text, ["ui", "api"])

        self.assertEqual(hits, ["api"])

    def test_whole_word_keywords_match_cyrillic_and_latin_boundaries(self):
        text = "Нужен бот для tg и API интеграции."

        hits = main.count_matches(text, ["бот", "tg", "api"])

        self.assertEqual(hits, ["бот", "tg", "api"])


class BuildFullMessageTests(unittest.TestCase):
    def test_relevant_message_includes_reply_draft(self):
        message, verdict = main.build_full_message(
            "Python telegram bot",
            "https://www.fl.ru/projects/123/test/",
            "Нужен python telegram бот для API интеграции и автоматизации.",
            "",
            False,
            "test source",
        )

        self.assertEqual(verdict, "✅ БРАТЬ")
        self.assertIn("✉️ Отклик (черновик)", message)
        self.assertIn("Здравствуйте!", message)

    def test_non_relevant_message_does_not_include_reply_draft(self):
        message, verdict = main.build_full_message(
            "Логотип и фирменный стиль",
            "https://www.fl.ru/projects/456/test/",
            "Нужен логотип, дизайн, figma и фирменный стиль.",
            "",
            False,
            "test source",
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertNotIn("✉️ Отклик (черновик)", message)
        self.assertNotIn("Здравствуйте!", message)


class ClaudeAnalyzeTests(unittest.TestCase):
    def test_missing_api_key_allows_order_without_external_call(self):
        with mock.patch.object(main.os, "getenv", return_value=""):
            self.assertTrue(main.claude_analyze("Title", "Description"))

    def test_no_answer_rejects_order(self):
        fake_client = SimpleNamespace(
            messages=SimpleNamespace(
                create=mock.Mock(
                    return_value=SimpleNamespace(
                        content=[SimpleNamespace(text="NO")]
                    )
                )
            )
        )
        fake_anthropic = SimpleNamespace(Anthropic=mock.Mock(return_value=fake_client))
        with mock.patch.object(main.os, "getenv", return_value="test-key"), \
                mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            self.assertFalse(main.claude_analyze("Logo", "Need design"))

        fake_anthropic.Anthropic.assert_called_once_with(api_key="test-key")
        fake_client.messages.create.assert_called_once()

    def test_api_exception_allows_order_to_avoid_dropping_jobs(self):
        fake_anthropic = SimpleNamespace(
            Anthropic=mock.Mock(side_effect=RuntimeError("network down"))
        )
        with mock.patch.object(main.os, "getenv", return_value="test-key"), \
                mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            self.assertTrue(main.claude_analyze("Python task", "Need backend"))


class EmailSendTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "EMAIL_TO": "to@example.com",
            "EMAIL_FROM": "from@example.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_SMTP": "smtp.example.com",
            "EMAIL_PORT": "587",
        }

    def getenv(self, key, default=""):
        return self.env.get(key, default)

    def test_missing_email_configuration_returns_false(self):
        with mock.patch.dict(main.os.environ, {}, clear=True):
            self.assertFalse(main.email_send("message"))

    def test_port_587_uses_starttls_smtp(self):
        server = mock.Mock()
        smtp_context = mock.Mock()
        smtp_context.__enter__ = mock.Mock(return_value=server)
        smtp_context.__exit__ = mock.Mock(return_value=False)

        with mock.patch.object(main.os, "getenv", side_effect=self.getenv), \
                mock.patch.object(main.smtplib, "SMTP", return_value=smtp_context) as smtp, \
                mock.patch.object(main.smtplib, "SMTP_SSL") as smtp_ssl:
            self.assertTrue(main.email_send("hello"))

        smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)
        smtp_ssl.assert_not_called()
        server.starttls.assert_called_once_with()
        server.login.assert_called_once_with("from@example.com", "secret")
        server.sendmail.assert_called_once()


if __name__ == "__main__":
    unittest.main()
