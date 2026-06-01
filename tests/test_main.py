import importlib.util
import logging
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


class ParsingTests(unittest.TestCase):
    def test_extract_id_from_project_path_query_or_hash_fallback(self):
        self.assertEqual(
            main.extract_id_from_link("https://www.fl.ru/projects/12345/sdelat-bota/"),
            "12345",
        )
        self.assertEqual(
            main.extract_id_from_link("https://www.fl.ru/projects/?pid=777&foo=bar"),
            "777",
        )

        unknown_link = "https://example.test/no-project-id"
        self.assertEqual(main.extract_id_from_link(unknown_link), main.sha1(unknown_link))

    def test_fetch_rss_items_unescapes_and_strips_html_description(self):
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss>
          <channel>
            <item>
              <title>Python &amp; API бот</title>
              <link>https://www.fl.ru/projects/555/test/</link>
              <description><![CDATA[
                <p>Проверить API &amp; бота</p>
                <script>alert("skip")</script>
                <br>детали
              ]]></description>
            </item>
          </channel>
        </rss>"""

        with patch.object(main, "http_get", return_value=(rss, 200)):
            items = main.fetch_rss_items()

        self.assertEqual(
            items,
            [
                {
                    "title": "Python & API бот",
                    "link": "https://www.fl.ru/projects/555/test/",
                    "desc": "Проверить API & бота детали",
                }
            ],
        )


class FilteringTests(unittest.TestCase):
    def test_whole_word_keywords_do_not_match_inside_larger_words(self):
        text = "Нужен email parser и xml выгрузка без machine learning."

        hits = main.count_matches(text, ["ml", "api", "parser"])

        self.assertNotIn("ml", hits)
        self.assertNotIn("api", hits)
        self.assertIn("parser", hits)

    def test_hard_design_keywords_override_core_hits(self):
        verdict, confidence, score = main.verdict_and_confidence(
            core_hits=["python", "api", "бот"],
            anti_hits=["дизайн", "figma"],
            text_len=1800,
        )

        self.assertEqual(verdict, "❌ НЕ РЕЛЕВАНТНО")
        self.assertEqual(confidence, 0.10)
        self.assertEqual(score, -10)

    def test_title_anti_filter_catches_marketing_without_blocking_bot_work(self):
        self.assertTrue(main.title_is_anti("Нужен маркетолог для продвижения канала"))
        self.assertFalse(main.title_is_anti("Telegram бот для отчётов по API"))


class TelegramTests(unittest.TestCase):
    def test_split_for_telegram_preserves_paragraphs_and_splits_long_blocks(self):
        chunks = main.split_for_telegram("first\n\nsecond\n\n" + ("x" * 12), limit=10)

        self.assertEqual(chunks, ["first", "second", "x" * 10, "xx"])

    def test_tg_send_posts_all_chunks_and_reports_partial_failure(self):
        responses = [Mock(status_code=200, text="ok"), Mock(status_code=500, text="bad")]

        with patch.object(main, "TELEGRAM_BOT_TOKEN", "token"), \
                patch.object(main, "TELEGRAM_CHAT_ID", "chat"), \
                patch.object(main, "SLEEP_BETWEEN_SENDS", 0), \
                patch.object(main, "split_for_telegram", return_value=["part one", "part two"]), \
                patch.object(main.requests, "post", side_effect=responses) as post, \
                patch.object(main.time, "sleep") as sleep:
            ok = main.tg_send("ignored")

        self.assertFalse(ok)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], "https://api.telegram.org/bottoken/sendMessage")
        self.assertEqual(post.call_args_list[0].kwargs["data"]["text"], "part one")
        self.assertEqual(post.call_args_list[1].kwargs["data"]["text"], "part two")
        self.assertEqual(sleep.call_count, 2)


class MainLoopTests(unittest.TestCase):
    def test_main_uses_rss_fallback_and_email_when_telegram_delivery_fails(self):
        rss_item = {
            "title": "Python API бот для отчетов",
            "link": "https://www.fl.ru/projects/901/python-api-bot/",
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
        mark_processed.assert_called_once_with(conn, "901", rss_item["link"], rss_item["title"])


if __name__ == "__main__":
    unittest.main()
