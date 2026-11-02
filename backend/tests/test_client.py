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
# 学校页面的真实形态：验证码容器默认 display:none，由服务端渲染的脚本显示并动态加入输入框
CAPTCHA_SCRIPT = ('<div class="form-group" style="display: none;" id="captchaParent"></div>'
                  '<script>vpn_eval((function(){ $("#captchaParent").show(); $("#captchaLabel").show(); '
                  "config.captcha = {\n    type: 'image',\n    id: '2132814849'\n}; "
                  "function captchaUrl() { return 'captcha?captchaId=' + config.captcha.id; } "
                  '} ).toString().slice(12, -2),"");</script>')
CAPTCHA_FORM = FORM + CAPTCHA_SCRIPT
ERR = '<div id="errorDiv" class="alert"><p>{}</p></div>'


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


class LoginBase(unittest.TestCase):
    def _router(self, post_status=302, post_body="", sso_form=FORM):
        holder = self.holder = {}

        def route(method, url, kw):
            logical = webvpn.logical_url(url) if url != "https://d.buaa.edu.cn/login" else None
            if method == "GET" and logical and logical.startswith("https://sso.buaa.edu.cn/captcha?captchaId="):
                holder.setdefault("captcha_gets", []).append(logical)
                holder["http"].cookies.set("JSESSIONID", "j1", domain="d.buaa.edu.cn")   # 验证码与会话绑定
                return resp(200, b"\x89PNGfake", {"Content-Type": "image/png;charset=UTF-8"})
            if method == "GET" and logical and logical.startswith("https://sso.buaa.edu.cn/login?service="):
                holder["login_gets"] = holder.get("login_gets", 0) + 1
                return resp(200, sso_form)
            if method == "POST":
                if "http" in holder:
                    holder["post_cookie"] = holder["http"].cookies.get("JSESSIONID")
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


class LoginTests(LoginBase):
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


class CaptchaLoginTests(LoginBase):
    """学校要求图片验证码：无人值守的 login() 不发送凭据；人工输入验证码的两步登录。"""

    def make(self, **kw):
        http = FakeHTTP(self._router(sso_form=kw.pop("sso_form", CAPTCHA_FORM), **kw))
        self.holder["http"] = http
        return Client(http), http

    def posts(self, http):
        return [x for x in http.calls if x[0] == "POST"]

    def test_detects_real_page_shape(self):
        self.assertTrue(webvpn.captcha_required(CAPTCHA_FORM))
        self.assertEqual(webvpn.captcha_id(CAPTCHA_FORM), "2132814849")
        self.assertFalse(webvpn.captcha_required(FORM))
        # 容器默认 display:none，静态判断看不出来（旧逻辑据此漏判）
        self.assertFalse(webvpn.needs_extra_auth_prompt(CAPTCHA_FORM))

    def test_captcha_id_charset_is_restricted(self):
        evil = CAPTCHA_FORM.replace("2132814849", "1&x=http://evil/")
        self.assertIsNone(webvpn.captcha_id(evil))

    def test_unattended_login_does_not_send_credentials(self):
        c, http = self.make()
        with self.assertRaises(ClientError) as cm:
            c.login("20371234", "pw")
        self.assertEqual(cm.exception.kind, "captcha")
        self.assertEqual(self.posts(http), [])    # 不带验证码只会被拒并留下失败记录

    def test_two_step_success_reuses_same_session(self):
        c, http = self.make()
        img, mime = c.begin_captcha_login()
        self.assertEqual((img, mime), (b"\x89PNGfake", "image/png"))
        self.assertEqual(self.posts(http), [])    # 第一步不发送任何凭据
        c.finish_captcha_login("20371234", "pw", "ab12")
        self.assertTrue(c.logged_in)
        post = self.posts(http)[0]
        self.assertEqual(post[2]["data"]["captcha"], "ab12")
        self.assertEqual(post[2]["data"]["execution"], "e1")          # 沿用第一步取到的表单，不重新取页面
        self.assertEqual(self.holder["login_gets"], 1)
        self.assertEqual(self.holder["post_cookie"], "j1")           # 第一步的 Cookie 没被清掉
        self.assertEqual(len(self.posts(http)), 1)
        self.assertEqual(self.holder["captcha_gets"][0].split("?")[0], "https://sso.buaa.edu.cn/captcha")
        self.assertIn("captchaId=2132814849", self.holder["captcha_gets"][0])
        self.assertFalse(any("ab12" in t or "pw" == t for t in c.trace))

    def test_wrong_captcha_is_captcha_error(self):
        c, _ = self.make(post_status=401, post_body=FORM + CAPTCHA_SCRIPT + ERR.format("Invalid captcha."))
        c.begin_captcha_login()
        with self.assertRaises(ClientError) as cm:
            c.finish_captcha_login("20371234", "pw", "zzzz")
        self.assertEqual(cm.exception.kind, "captcha")
        self.assertFalse(c.logged_in)

    def test_invalid_credentials_with_captcha_is_not_blamed_on_password_alone(self):
        c, _ = self.make(post_status=401, post_body=FORM + CAPTCHA_SCRIPT + ERR.format("Invalid credentials."))
        c.begin_captcha_login()
        with self.assertRaises(ClientError) as cm:
            c.finish_captcha_login("20371234", "pw", "zzzz")
        self.assertEqual(cm.exception.kind, "auth")
        self.assertIn("验证码", cm.exception.message)

    def test_token_is_single_use(self):
        c, http = self.make(post_status=401, post_body=FORM + CAPTCHA_SCRIPT + ERR.format("Invalid credentials."))
        c.begin_captcha_login()
        with self.assertRaises(ClientError):
            c.finish_captcha_login("20371234", "pw", "zzzz")
        with self.assertRaises(ClientError) as cm:
            c.finish_captcha_login("20371234", "pw", "zzzz")
        self.assertEqual(cm.exception.kind, "input")
        self.assertEqual(len(self.posts(http)), 1)    # 失败后不会拿旧表单再发一次

    def test_finish_without_begin(self):
        c, http = self.make()
        with self.assertRaises(ClientError) as cm:
            c.finish_captcha_login("20371234", "pw", "ab12")
        self.assertEqual(cm.exception.kind, "input")
        self.assertEqual(self.posts(http), [])

    def test_malformed_captcha_text_not_sent(self):
        c, http = self.make()
        c.begin_captcha_login()
        for bad in ("", "a b", "x" * 13, "ab&c=1"):
            c.begin_captcha_login()
            with self.assertRaises(ClientError) as cm:
                c.finish_captcha_login("20371234", "pw", bad)
            self.assertEqual(cm.exception.kind, "input")
        self.assertEqual(self.posts(http), [])

    def test_no_captcha_page_needs_no_code(self):
        c, http = self.make(sso_form=FORM)
        self.assertIsNone(c.begin_captcha_login())
        c.finish_captcha_login("20371234", "pw", "")
        self.assertTrue(c.logged_in)
        self.assertNotIn("captcha", self.posts(http)[0][2]["data"])

    def test_non_image_captcha_response_rejected(self):
        c, http = self.make()
        orig = http.router

        def route(m, u, k):
            if "captcha?captchaId" in (webvpn.logical_url(u) or ""):
                return resp(200, "<html>login</html>", {"Content-Type": "text/html"})
            return orig(m, u, k)
        http.router = route
        with self.assertRaises(ClientError) as cm:
            c.begin_captcha_login()
        self.assertEqual(cm.exception.kind, "protocol")


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
