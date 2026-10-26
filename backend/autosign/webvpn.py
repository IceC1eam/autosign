"""北航 WebVPN 网关地址映射、目标白名单与统一认证表单解析。

只允许学校已知服务；任何陌生主机/端口/带凭据的 URL 都会被拒绝。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:  # cryptography >= 43 把 CFB 挪到 decrepit，旧路径已弃用并将移除
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:
    from cryptography.hazmat.primitives.ciphers.modes import CFB

GATEWAY_HOST = "d.buaa.edu.cn"
GATEWAY = f"https://{GATEWAY_HOST}"

# wengine-vpn 的地址编码参数：AES-CFB，key 与 iv 相同。
# 这是网关公开的地址表示方式的一部分，不是账号密钥，也不能替代用户认证。
_KEY = _IV = b"wrdvpnisthebest!"

# (协议, 主机, 端口)
ALLOWED_TARGETS = {
    ("https", "sso.buaa.edu.cn", 443),
    ("https", "uc.buaa.edu.cn", 443),
    ("https", "iclass.buaa.edu.cn", 8346),
    ("https", "iclass.buaa.edu.cn", 8347),
    ("http", "iclass.buaa.edu.cn", 8081),
}
ALLOWED_HOSTS = {h for _, h, _ in ALLOWED_TARGETS}
# 门户自身仅允许的路径
PORTAL_PATHS = {"/login", "/logout", "/wengine-vpn-token-login", "/token-login"}
# 统一认证成功后，网关会把回调地址改写为“经网关访问门户自己”的形式
# （主机编码解出来是 d.buaa.edu.cn）。仅放行门户的已知路径。
PORTAL_TARGET = ("https", "d.buaa.edu.cn", 443)

_DEFAULT_PORT = {"http": 80, "https": 443}
_SEG_RE = re.compile(r"^(https?)(?:-(\d{1,5}))?$")
_HEX_RE = re.compile(r"^[0-9a-f]{34,}$")


class WebVPNError(Exception):
    """地址不在白名单、编码异常等。"""


def _enc_host(host: str) -> str:
    c = Cipher(algorithms.AES(_KEY), CFB(_IV)).encryptor()
    return _IV.hex() + (c.update(host.encode()) + c.finalize()).hex()


def _dec_host(token: str) -> str:
    if not _HEX_RE.match(token) or len(token) % 2:
        raise WebVPNError("主机编码格式异常")
    raw = bytes.fromhex(token)
    iv, body = raw[:16], raw[16:]
    c = Cipher(algorithms.AES(_KEY), CFB(iv)).decryptor()
    try:
        return (c.update(body) + c.finalize()).decode("ascii")
    except UnicodeDecodeError as e:
        raise WebVPNError("主机编码无法解码") from e


def _check_plain(url: str):
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise WebVPNError("URL 不允许携带用户名密码")
    if any(c in url for c in ("\r", "\n", "\t", " ", "\\")):
        raise WebVPNError("URL 含异常字符")
    return parts


def target_of(url: str, portal_any: bool = False) -> tuple[str, str, int]:
    """返回逻辑 URL 的 (协议, 主机, 端口)，并校验白名单。"""
    p = _check_plain(url)
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    try:
        port = p.port or _DEFAULT_PORT.get(scheme, 0)
    except ValueError as e:
        raise WebVPNError("端口异常") from e
    if (scheme, host, port) == PORTAL_TARGET:
        if p.path not in PORTAL_PATHS and not portal_any:
            raise WebVPNError(f"不允许的门户路径：{p.path}")
        return scheme, host, port
    if (scheme, host, port) not in ALLOWED_TARGETS:
        raise WebVPNError(f"不允许访问的目标：{scheme}://{host}:{port}")
    return scheme, host, port


def gateway_url(url: str) -> str:
    """逻辑地址 → 网关地址。"""
    scheme, host, port = target_of(url)
    p = urlsplit(url)
    seg = scheme if port == _DEFAULT_PORT[scheme] else f"{scheme}-{port}"
    path = p.path or "/"
    out = f"{GATEWAY}/{seg}/{_enc_host(host)}{path}"
    if p.query:
        out += "?" + p.query
    return out


def logical_url(url: str, portal_any: bool = False) -> str | None:
    """网关地址 → 逻辑地址；不是目标服务地址（如门户自身）则返回 None。"""
    p = _check_plain(url)
    if (p.hostname or "").lower() != GATEWAY_HOST or p.scheme != "https":
        raise WebVPNError("不是网关地址")
    segs = p.path.split("/", 3)  # ['', proto, token, rest]
    if len(segs) < 3:
        return None
    m = _SEG_RE.match(segs[1])
    if not m or not _HEX_RE.match(segs[2]):
        return None
    scheme = m.group(1)
    port = int(m.group(2)) if m.group(2) else _DEFAULT_PORT[scheme]
    host = _dec_host(segs[2])
    rest = "/" + segs[3] if len(segs) > 3 else "/"
    netloc = host if port == _DEFAULT_PORT[scheme] else f"{host}:{port}"
    out = f"{scheme}://{netloc}{rest}"
    if p.query:
        out += "?" + p.query
    target_of(out, portal_any)  # 校验白名单
    return out


def is_portal_page(gateway_url_: str) -> bool:
    """是否为门户自身（非目标服务）的页面。"""
    p = urlsplit(gateway_url_)
    if (p.hostname or "").lower() != GATEWAY_HOST:
        return False
    lg = logical_url(gateway_url_, portal_any=True)
    return lg is None or urlsplit(lg).hostname == GATEWAY_HOST


def portal_path(gateway_url_: str) -> str:
    """门户页面的实际路径（含“经网关访问门户自身”的形式）。"""
    lg = logical_url(gateway_url_, portal_any=True)
    return urlsplit(lg or gateway_url_).path


def describe(url: str) -> str:
    """用于诊断的脱敏描述：只保留协议/主机/端口/路径，不含查询参数。"""
    try:
        lg = logical_url(url) if url.startswith(GATEWAY) else None
    except WebVPNError:
        lg = None
    p = urlsplit(lg or url)
    host = p.hostname or "?"
    port = f":{p.port}" if p.port else ""
    keys = ",".join(sorted({k for k in (p.query or "").replace(";", "&").split("&")
                            for k in [k.split("=", 1)[0]] if k}))[:80]
    return f"{'gw:' if lg else ''}{p.scheme}://{host}{port}{p.path}"[:140] + (f"?[{keys}]" if keys else "")


def resolve_redirect(current_gateway_url: str, location: str, *, portal_any: bool = False) -> str:
    """把响应的 Location 解析为经过校验的网关 URL。

    - 相对/网关绝对地址：必须是门户的已知路径或白名单内的目标；
    - 未被改写的学校逻辑地址：校验后转换为网关地址；
    - 其余一律拒绝。
    """
    if any(c in location for c in ("\r", "\n")):
        raise WebVPNError("重定向地址含异常字符")
    nxt = urljoin(current_gateway_url, location)
    p = _check_plain(nxt)
    host = (p.hostname or "").lower()
    if host == GATEWAY_HOST:
        if p.scheme != "https":
            raise WebVPNError("网关地址必须使用 HTTPS")
        lg = logical_url(nxt, portal_any)
        if lg is None or urlsplit(lg).hostname == GATEWAY_HOST:
            path = p.path if lg is None else urlsplit(lg).path
            if path not in PORTAL_PATHS and not portal_any:
                raise WebVPNError(f"不允许的门户路径：{p.path}")
        return nxt
    if host in ALLOWED_HOSTS:
        return gateway_url(nxt)
    raise WebVPNError(f"重定向到陌生主机：{host}")


def resolve_form_action(page_gateway_url: str, action: str) -> str:
    """解析表单 action。未被网关改写的根相对路径基于逻辑 SSO 地址解析。"""
    if not action:
        return page_gateway_url
    page_logical = logical_url(page_gateway_url)
    if action.startswith("/") and not action.startswith("//") and page_logical:
        if not re.match(r"^/https?(-\d+)?/[0-9a-f]{34,}/", action):
            return gateway_url(urljoin(page_logical, action))
    return resolve_redirect(page_gateway_url, action)


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"id": a.get("id", ""), "action": a.get("action", ""),
                         "method": (a.get("method") or "get").lower(), "fields": {}}
            self.forms.append(self._cur)
        elif tag == "input" and self._cur is not None:
            name = a.get("name")
            if name:
                self._cur["fields"][name] = a.get("value", "") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def parse_forms(html: str) -> list[dict]:
    p = _FormParser()
    p.feed(html)
    return p.forms


def find_login_form(html: str) -> dict | None:
    for f in parse_forms(html):
        if "execution" in f["fields"] or f["id"] == "loginForm":
            return f
    return None


def sso_error_text(html: str) -> str:
    m = re.search(r'id="errorDiv"[^>]*>\s*<p>(.*?)</p>', html, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def needs_extra_auth_prompt(html: str) -> bool:
    """验证码 / 额外认证：交给用户处理，客户端不提供交互。

    统一认证页默认带有 display:none 的验证码容器，因此只在容器可见
    或错误提示明确提到验证码/锁定时才判定。
    """
    m = re.search(r'<div[^>]*id="captchaParent"[^>]*>', html)
    if m and "display: none" not in m.group(0).replace("display:none", "display: none"):
        return True
    return bool(re.search(r"captcha|验证码|validation code|locked|锁定|too many",
                          sso_error_text(html), re.I))
