import importlib.util
import logging
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "main.py"


def load_main_module():
    module_name = "fl_monitor_main"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)

    with mock.patch(
        "logging.FileHandler",
        side_effect=lambda *args, **kwargs: logging.NullHandler(),
    ):
        spec.loader.exec_module(module)

    return module


class ClaudeAnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_allows_project_when_anthropic_key_is_missing(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertTrue(self.main.claude_analyze("Parser task", "Build a parser"))

    def test_returns_false_for_no_response(self):
        calls = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text="NO")]
                )

        class FakeAnthropicClient:
            def __init__(self, api_key):
                self.api_key = api_key
                self.messages = FakeMessages()

        fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)
        long_text = "x" * 2100

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
                self.assertFalse(self.main.claude_analyze("Logo task", long_text))

        self.assertEqual("claude-haiku-4-5-20251001", calls[0]["model"])
        prompt = calls[0]["messages"][0]["content"]
        self.assertIn("x" * 2000, prompt)
        self.assertNotIn("x" * 2001, prompt)

    def test_allows_project_when_anthropic_call_fails(self):
        def raise_on_create(api_key):
            raise RuntimeError("network unavailable")

        fake_anthropic = types.SimpleNamespace(Anthropic=raise_on_create)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with mock.patch.dict(sys.modules, {"anthropic": fake_anthropic}):
                self.assertTrue(self.main.claude_analyze("Parser task", "Build parser"))


class MessageCompositionTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_irrelevant_message_does_not_include_reply_draft(self):
        with mock.patch.object(
            self.main, "build_reply_draft", return_value="DRAFT SHOULD NOT APPEAR"
        ) as draft:
            message, verdict = self.main.build_full_message(
                "Need figma UI",
                "https://example.test/project",
                "figma ui ux",
                "",
                False,
                "unit-test",
            )

        self.assertEqual("❌ НЕ РЕЛЕВАНТНО", verdict)
        draft.assert_not_called()
        self.assertNotIn("DRAFT SHOULD NOT APPEAR", message)

    def test_relevant_message_includes_reply_draft(self):
        with mock.patch.object(
            self.main, "build_reply_draft", return_value="DRAFT INCLUDED"
        ) as draft:
            message, verdict = self.main.build_full_message(
                "Python Telegram API bot",
                "https://example.test/project",
                "python telegram api bot automation parser",
                "",
                False,
                "unit-test",
            )

        self.assertEqual("✅ БРАТЬ", verdict)
        draft.assert_called_once()
        self.assertIn("DRAFT INCLUDED", message)


class HttpGetTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_successful_response_is_forced_to_utf8(self):
        response = types.SimpleNamespace(
            status_code=200,
            text="ok",
            encoding=None,
            raise_for_status=lambda: None,
        )

        with mock.patch.object(self.main.time, "time", return_value=123):
            with mock.patch.object(self.main.requests, "get", return_value=response) as get:
                text, status_code = self.main.http_get("https://example.test/feed")

        self.assertEqual(("ok", 200), (text, status_code))
        self.assertEqual("utf-8", response.encoding)
        self.assertEqual(
            "https://example.test/feed?_cb=123",
            get.call_args.args[0],
        )


class MainLoopClaudeGateTests(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_claude_rejection_marks_processed_without_sending_project(self):
        conn = object()
        project = {
            "title": "Python parser",
            "link": "https://www.fl.ru/projects/123/example/",
            "desc": "rss description",
        }

        with mock.patch.object(self.main, "db_connect", return_value=conn), \
            mock.patch.object(self.main, "fetch_projects_page", return_value=[project]), \
            mock.patch.object(self.main, "fetch_rss_items") as fetch_rss_items, \
            mock.patch.object(self.main, "is_processed", return_value=False), \
            mock.patch.object(self.main, "http_get", return_value=("<html></html>", 200)), \
            mock.patch.object(self.main, "try_extract_project_text", return_value="python parser"), \
            mock.patch.object(self.main, "claude_analyze", return_value=False), \
            mock.patch.object(self.main, "build_full_message") as build_full_message, \
            mock.patch.object(self.main, "mark_processed") as mark_processed, \
            mock.patch.object(self.main, "tg_send", return_value=True) as tg_send, \
            mock.patch.object(self.main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.main.main()

        fetch_rss_items.assert_not_called()
        build_full_message.assert_not_called()
        mark_processed.assert_called_once_with(
            conn,
            "123",
            "https://www.fl.ru/projects/123/example/",
            "Python parser",
        )
        self.assertEqual(1, tg_send.call_count)


if __name__ == "__main__":
    unittest.main()
