import unittest

from autosign import webvpn
from autosign.webvpn import WebVPNError

SSO = "https://sso.buaa.edu.cn/login"


class MappingTests(unittest.TestCase):
    def test_known_sso_vector(self):
        # 与真实网关核对过的编码
        self.assertEqual(
            webvpn.gateway_url(SSO),
            "https://d.buaa.edu.cn/https/77726476706e69737468656265737421"
            "e3e44ed225256951300d8db9d6562d/login")

    def test_port_segment_and_roundtrip(self):
        for u in ("https://iclass.buaa.edu.cn:8347/app/x.action?a=1&b=2",
                  "https://iclass.buaa.edu.cn:8346/?type=jumpMyCenter",
                  "http://iclass.buaa.edu.cn:8081/app/y",
                  "https://uc.buaa.edu.cn/"):
            g = webvpn.gateway_url(u)
            self.assertEqual(webvpn.logical_url(g), u)
        self.assertIn("/https-8347/", webvpn.gateway_url("https://iclass.buaa.edu.cn:8347/a"))
        self.assertIn("/http-8081/", webvpn.gateway_url("http://iclass.buaa.edu.cn:8081/a"))

    def test_rejects_unknown(self):
        for u in ("https://evil.example.com/", "https://iclass.buaa.edu.cn:9999/",
                  "http://sso.buaa.edu.cn/login", "https://user:pw@sso.buaa.edu.cn/login",
                  "https://sso.buaa.edu.cn/a b", "ftp://sso.buaa.edu.cn/"):
            with self.assertRaises(WebVPNError, msg=u):
                webvpn.gateway_url(u)


class RedirectTests(unittest.TestCase):
    cur = webvpn.gateway_url(SSO)

    def test_portal_paths(self):
        self.assertEqual(webvpn.resolve_redirect(self.cur, "https://d.buaa.edu.cn/login"),
                         "https://d.buaa.edu.cn/login")
        with self.assertRaises(WebVPNError):
            webvpn.resolve_redirect(self.cur, "https://d.buaa.edu.cn/evil")

    def test_relative_gateway_path(self):
        g = webvpn.gateway_url("https://iclass.buaa.edu.cn:8347/a")
        path = g[len("https://d.buaa.edu.cn"):]
        self.assertEqual(webvpn.resolve_redirect(self.cur, path), g)

    def test_logical_target_converted(self):
        self.assertEqual(
            webvpn.resolve_redirect(self.cur, "https://iclass.buaa.edu.cn:8346/x?y=1"),
            webvpn.gateway_url("https://iclass.buaa.edu.cn:8346/x?y=1"))

    def test_rejects_foreign(self):
        for loc in ("https://evil.example.com/", "http://d.buaa.edu.cn/login",
                    "https://sso.buaa.edu.cn.evil.com/", "//evil.example.com/x"):
            with self.assertRaises(WebVPNError, msg=loc):
                webvpn.resolve_redirect(self.cur, loc)


class NestedPortalTests(unittest.TestCase):
    """SSO 登录成功后，回调被改写成“经网关访问 d.buaa.edu.cn 自身”的形式。"""
    cur = webvpn.gateway_url(SSO)
    enc = webvpn._enc_host("d.buaa.edu.cn")

    def test_login_callback_allowed(self):
        loc = f"https://d.buaa.edu.cn/https/{self.enc}/login?cas_login=true&ticket=T"
        self.assertEqual(webvpn.resolve_redirect(self.cur, loc), loc)
        self.assertEqual(webvpn.logical_url(loc), "https://d.buaa.edu.cn/login?cas_login=true&ticket=T")

    def test_token_login_step_is_followed(self):
        loc = f"https://d.buaa.edu.cn/https/{self.enc}/wengine-vpn-token-login"
        self.assertEqual(webvpn.resolve_redirect(self.cur, loc), loc)

    def test_other_nested_paths_rejected(self):
        loc = f"https://d.buaa.edu.cn/https/{self.enc}/admin"
        with self.assertRaises(WebVPNError):
            webvpn.resolve_redirect(self.cur, loc)
        # 登录流程末段允许走到门户首页，但会被识别为门户页面而不再抓取
        nxt = webvpn.resolve_redirect(self.cur, f"/https/{self.enc}/", portal_any=True)
        self.assertTrue(webvpn.is_portal_page(nxt))
        self.assertEqual(webvpn.portal_path(nxt), "/")

    def test_not_a_general_target(self):
        with self.assertRaises(WebVPNError):
            webvpn.gateway_url("https://d.buaa.edu.cn/anything")


class FormTests(unittest.TestCase):
    HTML = ('<form id="loginForm" action="login" method="post">'
            '<input name="username"><input name="password" type="password">'
            '<input name="execution" value="abc"><input name="_eventId" value="submit"></form>')

    def test_parse(self):
        f = webvpn.find_login_form(self.HTML)
        self.assertEqual(f["fields"]["execution"], "abc")
        self.assertEqual(f["action"], "login")

    def test_action_relative(self):
        page = webvpn.gateway_url(SSO + "?service=x")
        self.assertEqual(webvpn.resolve_form_action(page, "login"),
                         webvpn.gateway_url(SSO))

    def test_action_root_relative_uses_logical_sso(self):
        page = webvpn.gateway_url(SSO)
        self.assertEqual(webvpn.resolve_form_action(page, "/other"),
                         webvpn.gateway_url("https://sso.buaa.edu.cn/other"))

    def test_action_foreign_rejected(self):
        page = webvpn.gateway_url(SSO)
        with self.assertRaises(WebVPNError):
            webvpn.resolve_form_action(page, "https://evil.example.com/steal")

    def test_captcha_detection(self):
        hidden = '<div style="display: none;" id="captchaParent"></div>'
        shown = '<div id="captchaParent"></div>'
        self.assertFalse(webvpn.needs_extra_auth_prompt(hidden))
        self.assertTrue(webvpn.needs_extra_auth_prompt(shown))
        self.assertTrue(webvpn.needs_extra_auth_prompt(
            hidden + '<div id="errorDiv"><p>Please input captcha</p></div>'))
        self.assertFalse(webvpn.needs_extra_auth_prompt(
            hidden + '<div id="errorDiv"><p>Invalid credentials.</p></div>'))


if __name__ == "__main__":
    unittest.main()
