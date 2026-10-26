import json
import unittest
from datetime import date, datetime, timedelta

import requests

from autosign import iclass, webvpn
from autosign.iclass import CHINA, Client, ClientError, SessionExpired

FORM = ('<form id="loginForm" action="login" method="post"><input name="username">'
        '<input name="password"><input name="execution" value="e1">'
        '<input name="_eventId" value="submit"></form>')
SSO_PAGE = webvpn.gateway_url("https://sso.buaa.edu.cn/login")


def resp(status=200, body="", headers=None, url=""):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode() if isinstance(body, str) else body
    r.headers.update(headers or {})
    r.url = url
    return r


class FakeHTTP:
    """按顺序/按路由返回预置响应，并记录请求。"""

    def __init__(self, router):
        self.router = router
        self.calls = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers = {}
        self.trust_env = False

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.router(method, url, kw)


def lesson(i, start, signed=0):
    return {"id": i, "courseName": "高等数学", "teacherName": "张老师", "classroomName": "J4-101",
            "classBeginTime": start.strftime("%Y-%m-%d %H:%M:%S"),
            "classEndTime": (start + timedelta(minutes=95)).strftime("%Y-%m-%d %H:%M:%S"),
            "signStatus": str(signed)}


class ParseTests(unittest.TestCase):
    def test_sign_status_unknown_not_unsigned(self):
        t = datetime(2026, 10, 18, 8, 0, tzinfo=CHINA)
        ls = iclass.parse_lessons({"result": [lesson("1", t, 0), lesson("2", t, 1),
                                              {**lesson("3", t), "signStatus": "7"}]})
        self.assertEqual([x.sign_status for x in ls], [0, 1, None])

    def test_bad_structure(self):
        with self.assertRaises(ClientError):
            iclass.parse_lessons({"result": "oops"})

    def test_window(self):
        t = datetime(2026, 10, 18, 8, 0, tzinfo=CHINA)
        L = iclass.parse_lessons({"result": [lesson("1", t)]})[0]
        self.assertFalse(L.window_open(t - timedelta(minutes=11)))
        self.assertTrue(L.window_open(t - timedelta(minutes=10)))
        self.assertTrue(L.window_open(L.end))
        self.assertFalse(L.window_open(L.end + timedelta(seconds=1)))


class LoginTests(unittest.TestCase):
    def _router(self, post_status=302, post_body="", sso_form=FORM):
        holder = self.holder = {}

        def route(method, url, kw):
            logical = webvpn.logical_url(url) if url != "https://d.buaa.edu.cn/login" else None
            if method == "GET" and logical and logical.startswith("https://sso.buaa.edu.cn/login?service="):
                return resp(200, sso_form)
            if method == "POST":
                if post_status == 302:
                    holder["http"].cookies.set("wengine_vpn_ticket", "x", domain="d.buaa.edu.cn")
                    return resp(302, headers={"Location": "https://d.buaa.edu.cn/login?cas_login=true&ticket=T"})
                return resp(post_status, post_body or FORM)
            if method == "GET" and url.startswith("https://d.buaa.edu.cn/login?cas_login"):
                return resp(302, headers={"Location": "/"})  # 门户首页：不应被抓取
            if method == "GET" and logical and ":8346" in logical:
                if "loginName=" in logical:
                    return resp(200, "<html>center</html>")
                return resp(302, headers={"Location": url.replace("?type=jumpMyCenter", "") + "?loginName=20371234"})
            if method == "GET" and logical and "/app/user/login.action" in logical:
                return resp(200, json.dumps({"STATUS": "0", "result": {"id": "99", "sessionId": "S"}}),
                            {"Content-Type": "application/json"})
            return resp(404)
        return route

    def test_success(self):
        http = FakeHTTP(self._router())
        self.holder["http"] = http
        c = Client(http)
        c.login("20371234", "pw")
        self.assertFalse([x for x in http.calls if x[1] == "https://d.buaa.edu.cn/"])
        self.assertTrue(any("gw:https://sso.buaa.edu.cn/login" in t for t in c.trace))
        self.assertFalse(any("ticket=" in t or "20371234" in t.replace("loginName 来源", "") for t in c.trace))
        self.assertEqual((c.user_id, c.session_id, c.login_name), ("99", "S", "20371234"))
        posts = [x for x in http.calls if x[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["data"]["execution"], "e1")
        self.assertEqual(webvpn.logical_url(posts[0][1]), "https://sso.buaa.edu.cn/login")

    def test_wrong_password(self):
        c = Client(FakeHTTP(self._router(post_status=401)))
        with self.assertRaises(ClientError) as cm:
            c.login("20371234", "bad")
        self.assertEqual(cm.exception.kind, "auth")
        self.assertFalse(c.logged_in)

    def test_307_not_replayed(self):
        def route(method, url, kw):
            if method == "POST":
                return resp(307, headers={"Location": "https://evil.example.com/"})
            return resp(200, FORM)
        http = FakeHTTP(route)
        with self.assertRaises(ClientError):
            Client(http).login("20371234", "pw")
        self.assertEqual(len([x for x in http.calls if x[0] == "POST"]), 1)

    def test_foreign_form_action_gets_no_password(self):
        evil = FORM.replace('action="login"', 'action="https://evil.example.com/x"')
        http = FakeHTTP(lambda m, u, k: resp(200, evil))
        with self.assertRaises(ClientError):
            Client(http).login("20371234", "pw")
        self.assertFalse([x for x in http.calls if x[0] == "POST"])

    def test_failed_login_clears_old_identity(self):
        c = Client(FakeHTTP(self._router(post_status=401)))
        c.user_id, c.session_id = "old", "old"
        with self.assertRaises(ClientError):
            c.login("20371234", "bad")
        self.assertIsNone(c.user_id)


class ApiTests(unittest.TestCase):
    def client(self, router):
        http = FakeHTTP(router)
        c = Client(http)
        c.user_id, c.session_id = "99", "S"
        return c, http

    def test_html_means_session_expired_not_empty(self):
        c, _ = self.client(lambda m, u, k: resp(200, FORM, {"Content-Type": "text/html"}))
        with self.assertRaises(SessionExpired):
            c.schedules(date(2026, 10, 18))

    def test_redirect_means_session_expired(self):
        c, _ = self.client(lambda m, u, k: resp(302, headers={"Location": "/x"}))
        with self.assertRaises(SessionExpired):
            c.schedules(date(2026, 10, 18))

    def test_read_retries_only_connection_errors(self):
        n = {"i": 0}

        def route(m, u, k):
            n["i"] += 1
            if n["i"] < 3:
                raise requests.ConnectionError("x")
            return resp(200, json.dumps({"STATUS": "0", "result": []}), {"Content-Type": "application/json"})
        c, _ = self.client(route)
        self.assertEqual(c.schedules(date(2026, 10, 18)), [])
        self.assertEqual(n["i"], 3)

    def test_no_lessons_status_2_is_empty(self):
        c, _ = self.client(lambda m, u, k: resp(200, json.dumps({"STATUS": "2"}),
                                                {"Content-Type": "application/json"}))
        self.assertEqual(c.schedules(date(2026, 10, 18)), [])

    def test_business_error_not_retried(self):
        n = {"i": 0}

        def route(m, u, k):
            n["i"] += 1
            return resp(200, json.dumps({"STATUS": "1"}), {"Content-Type": "application/json"})
        c, _ = self.client(route)
        with self.assertRaises(ClientError):
            c.schedules(date(2026, 10, 18))
        self.assertEqual(n["i"], 1)


class SignTests(unittest.TestCase):
    def setUp(self):
        self.now = iclass.now_china()
        self.start = (self.now - timedelta(minutes=5)).replace(microsecond=0)
        self.day = self.now.date()

    def make(self, sign_resp, signed_after=0, signed_before=0):
        state = {"sign_posts": 0, "scheds": 0}

        def route(m, u, k):
            logical = webvpn.logical_url(u)
            js = {"Content-Type": "application/json"}
            if "get_stu_course_sched" in logical:
                state["scheds"] += 1
                s = signed_before if state["scheds"] == 1 else signed_after
                return resp(200, json.dumps({"STATUS": "0", "result": [lesson("5", self.start, s)]}), js)
            if "get_timestamp" in logical:
                return resp(200, json.dumps({"STATUS": "0", "timestamp": 1760000000000}), js)
            if "stu_scan_sign" in logical:
                state["sign_posts"] += 1
                self.assertEqual(m, "POST")
                self.assertEqual(k["data"], {"id": "99"})
                self.assertEqual(k["params"]["courseSchedId"], "5")
                self.assertEqual(k["headers"]["Sessionid"], "S")
                return sign_resp()
            return resp(404)
        http = FakeHTTP(route)
        c = Client(http)
        c.user_id, c.session_id = "99", "S"
        return c, state

    def test_success(self):
        c, st = self.make(lambda: resp(200, json.dumps(
            {"STATUS": "0", "result": {"stuSignStatus": 1}}), {"Content-Type": "application/json"}))
        self.assertEqual(c.sign(self.day, "5").status, "success")
        self.assertEqual(st["sign_posts"], 1)

    def test_lost_response_requeries_never_resends(self):
        def boom():
            raise requests.ReadTimeout("t")
        c, st = self.make(boom, signed_after=1)
        self.assertEqual(c.sign(self.day, "5").status, "success")
        self.assertEqual(st["sign_posts"], 1)

    def test_lost_response_unconfirmed_is_unknown(self):
        def boom():
            raise requests.ReadTimeout("t")
        c, st = self.make(boom, signed_after=0)
        self.assertEqual(c.sign(self.day, "5").status, "unknown")
        self.assertEqual(st["sign_posts"], 1)

    def test_business_failure(self):
        c, _ = self.make(lambda: resp(200, json.dumps({"STATUS": "1"}), {"Content-Type": "application/json"}))
        self.assertEqual(c.sign(self.day, "5").status, "failed")

    def test_already_signed_no_post(self):
        c, st = self.make(lambda: resp(500), signed_before=1)
        self.assertEqual(c.sign(self.day, "5").status, "success")
        self.assertEqual(st["sign_posts"], 0)

    def test_outside_window_refused(self):
        self.start = self.now + timedelta(hours=3)
        c, st = self.make(lambda: resp(500))
        with self.assertRaises(ClientError):
            c.sign(self.day, "5")
        self.assertEqual(st["sign_posts"], 0)


if __name__ == "__main__":
    unittest.main()
