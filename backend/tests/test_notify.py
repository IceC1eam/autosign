import os
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["APP_DATA"] = TMP

import requests  # noqa: E402

from autosign import db, notify  # noqa: E402

KEY = "SCT" + "a1B2c3D4e5" * 3


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class NotifyTests(unittest.TestCase):
    def setUp(self):
        # 各测试模块在导入时都改 APP_DATA（同进程里后导入者生效）：这里显式用自己的目录，结束后还原
        self._data = os.environ.get("APP_DATA")
        os.environ["APP_DATA"] = TMP
        db.reset_for_tests()
        for f in os.listdir(TMP):
            os.remove(os.path.join(TMP, f))
        os.environ[notify.ENV_KEY] = KEY
        self._d = notify._dispatch
        notify._dispatch = lambda fn, *a: fn(*a)       # 同步，便于断言

    def tearDown(self):
        notify._dispatch = self._d
        os.environ.pop(notify.ENV_KEY, None)
        db.reset_for_tests()
        if self._data is None:
            os.environ.pop("APP_DATA", None)
        else:
            os.environ["APP_DATA"] = self._data

    def test_unconfigured_is_noop_and_does_not_claim(self):
        os.environ.pop(notify.ENV_KEY)
        with mock.patch.object(notify.requests, "post") as post:
            self.assertFalse(notify.send("k", "t", "b"))
            post.assert_not_called()
        os.environ[notify.ENV_KEY] = KEY               # 之后配上密钥，仍成立的事件还能补发
        with mock.patch.object(notify.requests, "post", return_value=Resp()) as post:
            self.assertTrue(notify.send("k", "t", "b"))
            post.assert_called_once()

    def test_invalid_key_is_rejected(self):
        for bad in ("short", KEY + "/../x", KEY + "?a=b", "SCT key with space 12345"):
            os.environ[notify.ENV_KEY] = bad
            self.assertFalse(notify.configured(), bad)
            self.assertFalse(notify.send("k", "t", "b"))

    def test_endpoint_for_both_serverchan_flavours(self):
        self.assertEqual(notify._endpoint(KEY), f"https://sctapi.ftqq.com/{KEY}.send")
        self.assertEqual(notify._endpoint("sctp1234tABCDEF123456"),
                         "https://1234.push.ft07.com/send/sctp1234tABCDEF123456.send")

    def test_dedupe_within_cooldown_then_again_after(self):
        with mock.patch.object(notify.requests, "post", return_value=Resp()) as post:
            self.assertTrue(notify.send("k", "t", "b", cooldown=3600))
            self.assertFalse(notify.send("k", "t", "b", cooldown=3600))
            self.assertTrue(notify.send("other", "t", "b", cooldown=3600))
            self.assertEqual(post.call_count, 2)
            db.x("UPDATE notices SET ts=ts-7200")
            self.assertTrue(notify.send("k", "t", "b", cooldown=3600))

    def test_title_truncated_and_post_args(self):
        with mock.patch.object(notify.requests, "post", return_value=Resp()) as post:
            notify.send("k", "题" * 80, "body")
            args, kw = post.call_args
            self.assertEqual(len(kw["data"]["title"]), 32)
            self.assertEqual(kw["data"]["desp"], "body")
            self.assertEqual(kw["timeout"], notify.TIMEOUT)
            self.assertFalse(kw["allow_redirects"])

    def test_network_error_never_raises_and_never_leaks_key(self):
        err = requests.ConnectionError(f"HTTPSConnectionPool: https://sctapi.ftqq.com/{KEY}.send failed")
        with mock.patch.object(notify.requests, "post", side_effect=err):
            self.assertTrue(notify.send("k", "t", "b", account_id=1))   # 已安排发送；失败只记录
        msgs = " ".join(r["message"] for r in db.q("SELECT message FROM events"))
        self.assertIn("通知发送失败", msgs)
        self.assertIn("ConnectionError", msgs)
        self.assertNotIn(KEY, msgs)

    def test_api_level_error_is_recorded(self):
        with mock.patch.object(notify.requests, "post", return_value=Resp(200, {"code": 40001, "message": "bad"})):
            notify.send("k", "t", "b", account_id=1)
        self.assertIn("code=40001", db.one("SELECT message FROM events")["message"])
        with mock.patch.object(notify.requests, "post", return_value=Resp(500)):
            notify.send("k2", "t", "b", account_id=1)
        self.assertIn("HTTP 500", db.q("SELECT message FROM events ORDER BY id DESC")[0]["message"])

    def test_dispatch_failure_does_not_propagate(self):
        def boom(*a):
            raise RuntimeError("pool down")
        notify._dispatch = boom
        self.assertFalse(notify.send("k", "t", "b"))


if __name__ == "__main__":
    unittest.main()
