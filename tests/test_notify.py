"""Telegram + ntfy command channel tests (HTTP is faked)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signal_agent import notify  # noqa: E402
from signal_agent.config import Secrets  # noqa: E402

TOKEN = "123456789:AAH" + "x" * 32


class Resp:
    def __init__(self, data=None, text=""):
        self._d, self.text, self.ok, self.status_code = data, text, True, 200

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class FakeHTTP:
    def __init__(self, ntfy_msgs, chat_id=777, token_ok=True):
        self.ntfy_msgs, self.chat_id, self.posts, self.token_ok = ntfy_msgs, chat_id, [], token_ok

    def get(self, url, params=None, timeout=None):
        if url.endswith("/json"):
            return Resp(text="\n".join(json.dumps(m) for m in self.ntfy_msgs))
        if url.endswith("/getMe"):
            r = Resp({"ok": self.token_ok}); r.status_code = 200 if self.token_ok else 401
            return r
        if url.endswith("/getUpdates"):
            res = [{"message": {"chat": {"id": self.chat_id, "type": "private"}}}] if self.chat_id else []
            return Resp({"ok": True, "result": res})
        raise AssertionError(url)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return Resp({"ok": True})


class TestTelegram(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NOTIFY_SETTINGS_PATH"] = str(Path(self.tmp.name) / "notify.json")
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        self.sec = Secrets()
        self.sec.ntfy_topic, self.sec.ntfy_server, self.sec.dry_run = "signals-test", "https://ntfy.sh", False

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("NOTIFY_SETTINGS_PATH", None)

    def run_with(self, http, fn, *a):
        with mock.patch.object(notify.requests, "get", http.get), mock.patch.object(notify.requests, "post", http.post):
            return fn(*a)

    def test_token_command_connects_and_alerts_go_to_both(self):
        http = FakeHTTP([{"event": "open"},
                         {"event": "message", "id": "m1", "message": "hello"},
                         {"event": "message", "id": "m2", "message": f"telegram {TOKEN}"}])
        done = self.run_with(http, notify.poll_commands, self.sec)
        self.assertEqual(done, ["telegram connected"])
        s = notify.load_settings()
        self.assertEqual((s["telegram_token"], s["telegram_chat_id"], s["ntfy_since"]), (TOKEN, 777, "m2"))
        tg_posts = [p for p in http.posts if "sendMessage" in p[0]]
        self.assertEqual(tg_posts[0][1]["chat_id"], 777)

        http2 = FakeHTTP([])
        self.run_with(http2, notify.push, self.sec, "BUY CALL · NVDA", "details <b>", ["x"], 5, None,
                      [{"label": "Chart", "url": "https://example.com"}])
        urls = [p[0] for p in http2.posts]
        self.assertIn("https://ntfy.sh", urls)
        tg = [p[1] for p in http2.posts if "sendMessage" in p[0]][0]
        self.assertIn("&lt;b&gt;", tg["text"])            # user text is escaped
        self.assertFalse(tg["disable_notification"])     # priority 5 rings
        self.assertEqual(tg["reply_markup"]["inline_keyboard"][0][0]["text"], "Chart")

    def test_no_chat_yet_saves_token_and_tells_user(self):
        http = FakeHTTP([{"event": "message", "id": "a", "message": f"Telegram {TOKEN}"}], chat_id=None)
        done = self.run_with(http, notify.poll_commands, self.sec)
        self.assertEqual(done, ["telegram token saved, chat not found"])
        self.assertTrue(any("almost there" in (p[1].get("title") or "") for p in http.posts))
        # later the user taps Start → next alert discovers the chat automatically
        http2 = FakeHTTP([], chat_id=999)
        self.run_with(http2, notify.push, self.sec, "t", "m")
        self.assertEqual(notify.load_settings()["telegram_chat_id"], 999)

    def test_alert_text_is_not_mistaken_for_a_command(self):
        http = FakeHTTP([{"event": "message", "id": "z", "message": "BUY PUT · SNXX telegram 123"}])
        self.assertEqual(self.run_with(http, notify.poll_commands, self.sec), [])
        self.assertNotIn("telegram_token", notify.load_settings())

    def test_bad_key_is_reported(self):
        http = FakeHTTP([{"event": "message", "id": "k", "message": f"telegram {TOKEN}"}], token_ok=False)
        self.assertEqual(self.run_with(http, notify.poll_commands, self.sec), ["telegram key rejected"])
        self.assertNotIn("telegram_token", notify.load_settings())
        self.assertTrue(any("not accepted" in (p[1].get("title") or "") for p in http.posts))

    def test_late_start_connects_on_next_poll(self):
        notify.save_settings({"telegram_token": TOKEN, "ntfy_since": "x"})
        http = FakeHTTP([], chat_id=555)
        self.assertEqual(self.run_with(http, notify.poll_commands, self.sec), ["telegram connected (late)"])
        self.assertEqual(notify.load_settings()["telegram_chat_id"], 555)

    def test_off(self):
        notify.save_settings({"telegram_token": TOKEN, "telegram_chat_id": 1})
        http = FakeHTTP([{"event": "message", "id": "q", "message": "telegram off"}])
        self.run_with(http, notify.poll_commands, self.sec)
        self.assertNotIn("telegram_token", notify.load_settings())


if __name__ == "__main__":
    unittest.main(verbosity=2)
