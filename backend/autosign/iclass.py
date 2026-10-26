"""北航 iClass（经 WebVPN）客户端：双阶段登录、课表查询、手动签到。

约束（来自说明文档）：
- 读取请求仅对连接类故障有限重试（最多 3 次）；
- 签到 POST 只提交一次，之后只回查课表确认；
- 认证失败/业务错误/结构异常不会被当成“今天没课”；
- 不记录完整 URL、响应正文、表单或令牌。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlsplit

import requests

from . import webvpn
from .webvpn import WebVPNError

CHINA = timezone(timedelta(hours=8))
MAX_HOPS = 12
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SSO_LOGIN = "https://sso.buaa.edu.cn/login"
PORTAL_CALLBACK = "https://d.buaa.edu.cn/login?cas_login=true"
ICLASS_ENTRY = "https://iclass.buaa.edu.cn:8346/?type=jumpMyCenter"
API = "https://iclass.buaa.edu.cn:8347"
SIGN_API = "http://iclass.buaa.edu.cn:8081"


class ClientError(Exception):
    """kind: auth | captcha | session | network | business | protocol | input"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.trace: list[str] = []


class SessionExpired(ClientError):
    def __init__(self, message: str = "iClass 会话已失效，请重新登录"):
        super().__init__("session", message)


@dataclass
class Lesson:
    id: str
    name: str
    teacher: str
    room: str
    start: datetime
    end: datetime
    sign_status: int | None  # 0 未签到 / 1 已签到 / None 未知

    @property
    def slot(self) -> tuple[str, float]:
        """同名同时段的课程（仅教师/教室不同）视为同一节课，共用同一个签到时刻。"""
        return " ".join(self.name.split()), self.start.timestamp()

    def window_open(self, now: datetime) -> bool:
        return self.start - timedelta(minutes=10) <= now <= self.end

    def to_dict(self, now: datetime) -> dict:
        state = {0: "unsigned", 1: "signed"}.get(self.sign_status, "unknown")
        return {
            "id": self.id, "name": self.name, "teacher": self.teacher, "room": self.room,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "state": state,
            "can_sign": self.sign_status == 0 and self.window_open(now),
        }


@dataclass
class SignResult:
    status: str  # success | failed | unknown
    message: str


def now_china() -> datetime:
    return datetime.now(CHINA)


def _pick(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _parse_time(v) -> datetime:
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CHINA)
        except ValueError:
            continue
    raise ClientError("protocol", "课表时间格式无法识别")


def parse_lessons(payload: dict) -> list[Lesson]:
    result = payload.get("result")
    if result is None:
        return []
    if not isinstance(result, list):
        raise ClientError("protocol", "课表响应结构异常")
    out = []
    for it in result:
        if not isinstance(it, dict):
            raise ClientError("protocol", "课表条目结构异常")
        lid = _pick(it, "id", "courseSchedId")
        if lid == "":
            raise ClientError("protocol", "课表条目缺少 ID")
        raw = str(_pick(it, "signStatus", default="")).strip()
        sign = int(raw) if raw in ("0", "1") else None  # 其他取值标记为未知，不猜测为未签到
        out.append(Lesson(
            id=str(lid),
            name=str(_pick(it, "courseName", "name", default="(未命名课程)")),
            teacher=str(_pick(it, "teacherName", "teacher")),
            room=str(_pick(it, "classroomName", "roomName", "classroom", "place")),
            start=_parse_time(_pick(it, "classBeginTime", "beginTime", "startTime")),
            end=_parse_time(_pick(it, "classEndTime", "endTime")),
            sign_status=sign,
        ))
    out.sort(key=lambda x: x.start)
    return out


class Client:
    def __init__(self, session: requests.Session | None = None):
        self.http = session or requests.Session()
        self.http.trust_env = False
        self.http.headers["User-Agent"] = UA
        self.user_id: str | None = None
        self.session_id: str | None = None
        self.login_name: str | None = None
        self.trace: list[str] = []

    # ---------- 状态 ----------
    @property
    def logged_in(self) -> bool:
        return bool(self.user_id and self.session_id)

    def clear(self):
        self.user_id = self.session_id = self.login_name = None
        self.http.cookies.clear()

    # ---------- 底层 ----------
    def _send(self, method: str, gw_url: str, **kw) -> requests.Response:
        try:
            r = self.http.request(method, gw_url, allow_redirects=False,
                                  timeout=(8, 25), verify=True, **kw)
        except requests.RequestException as e:
            self._note(method, gw_url, type(e).__name__)
            raise ClientError("network", f"网络请求失败（{type(e).__name__}）") from None
        loc = r.headers.get("Location") if 300 <= r.status_code < 400 else None
        self._note(method, gw_url, r.status_code, loc)
        return r

    def _note(self, method, gw_url, status, loc=None):
        """记录脱敏的请求轨迹（无查询参数、无正文），登录失败时用于诊断。"""
        line = f"{method} {webvpn.describe(gw_url)} -> {status}"
        if loc:
            try:
                nxt = webvpn.urljoin(gw_url, loc)
            except Exception:
                nxt = loc
            line += f" => {webvpn.describe(nxt)}"
        self.trace.append(line)
        del self.trace[:-40]

    def _walk(self, gw_url: str, hops: int = MAX_HOPS, stop_at_portal: bool = False):
        """手动跟随经过校验的重定向。返回 (响应, 最终网关 URL, 途经的网关 URL 列表)。"""
        trail = [gw_url]
        for _ in range(hops + 1):
            r = self._send("GET", gw_url)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                if not loc:
                    raise ClientError("protocol", "重定向缺少 Location")
                try:
                    nxt = webvpn.resolve_redirect(gw_url, loc, portal_any=stop_at_portal)
                except WebVPNError as e:
                    raise ClientError("protocol", f"拒绝不安全的重定向：{e}") from None
                trail.append(nxt)
                if stop_at_portal and webvpn.is_portal_page(nxt)                         and webvpn.portal_path(nxt) not in webvpn.PORTAL_PATHS:
                    return r, nxt, trail  # 已回到门户首页：网关会话已建立，无需再加载
                gw_url = nxt
                continue
            return r, gw_url, trail
        raise ClientError("protocol", "重定向次数过多")

    # ---------- 阶段一：WebVPN + 统一认证 ----------
    def _gateway_login(self, number: str, password: str):
        start = webvpn.gateway_url(f"{SSO_LOGIN}?service={quote(PORTAL_CALLBACK, safe='')}")
        r, page_url, _ = self._walk(start)
        form = webvpn.find_login_form(r.text)
        if not form:
            raise ClientError("protocol", "未找到统一认证登录表单")
        try:
            action = webvpn.resolve_form_action(page_url, form["action"])
            logical = webvpn.logical_url(action)
        except WebVPNError as e:
            raise ClientError("protocol", f"登录表单目标异常：{e}") from None
        if not logical or urlsplit(logical).hostname != "sso.buaa.edu.cn":
            raise ClientError("protocol", "登录表单目标不是学校统一认证，已拒绝发送凭据")
        data = dict(form["fields"])
        data.update(username=number, password=password)
        r = self._send("POST", action, data=data)
        if r.status_code in (307, 308):
            raise ClientError("protocol", "统一认证返回 307/308，已停止以避免重放密码")
        if r.status_code in (301, 302, 303):
            try:
                nxt = webvpn.resolve_redirect(action, r.headers.get("Location", ""))
            except WebVPNError as e:
                raise ClientError("protocol", f"拒绝不安全的重定向：{e}") from None
            r, _, _ = self._walk(nxt, MAX_HOPS - 1, stop_at_portal=True)
            if webvpn.find_login_form(r.text):
                raise ClientError("auth", "统一认证未通过")
            if r.status_code >= 400:
                raise ClientError("protocol", f"登录回调异常（HTTP {r.status_code}）")
            if not any(c.domain.endswith(webvpn.GATEWAY_HOST) for c in self.http.cookies):
                raise ClientError("protocol", "认证通过但未取得 WebVPN 网关会话 Cookie")
            return
        # 未发生跳转：账号或密码错误，或需要验证码
        if r.status_code in (200, 401):
            if webvpn.needs_extra_auth_prompt(r.text):
                raise ClientError(
                    "captcha", "统一认证要求验证码/额外认证，请先在浏览器登录一次后重试")
            err = webvpn.sso_error_text(r.text)
            raise ClientError("auth", "学号或密码错误" + (f"（{err}）" if err else ""))
        raise ClientError("protocol", f"统一认证响应异常（HTTP {r.status_code}）")

    # ---------- 阶段二：iClass 业务会话 ----------
    def _find_login_name(self, r: requests.Response, trail: list[str]) -> str | None:
        for u in trail:
            try:
                q = parse_qs(urlsplit(u).query)
                if u.startswith(webvpn.GATEWAY):
                    lg = webvpn.logical_url(u)
                    if lg:
                        q.update(parse_qs(urlsplit(lg).query))
            except (WebVPNError, ValueError):
                continue
            for k in ("loginName", "loginname"):
                if q.get(k) and re.fullmatch(r"\S{1,512}", q[k][0]):
                    return q[k][0]
        m = re.search(r"loginName[\"'\s:=]+([A-Za-z0-9_\-]{3,40})", r.text or "")
        return m.group(1) if m else None

    def _business_login(self, number: str):
        r, _, trail = self._walk(webvpn.gateway_url(ICLASS_ENTRY))
        if webvpn.find_login_form(r.text):
            raise ClientError("session", "进入 iClass 时仍停留在统一认证页，WebVPN 会话未建立")
        found = self._find_login_name(r, trail)
        login_name = found or number
        self.login_name = login_name
        names = sorted(set(re.findall(r"[\"']?([A-Za-z]*[Ll]ogin[A-Za-z]*)[\"']?\s*[:=]", r.text or "")))[:8]
        self.trace.append(f"loginName 来源={'URL/页面' if found else '回退为学号'}；页面长度={len(r.text or '')}；"
                          f"页面含login相关字段={names}")
        params = {"phone": login_name, "verificationType": "2", "userLevel": "1",
                  "password": "", "verificationUrl": ""}
        payload = self._api("GET", f"{API}/app/user/login.action", params=params,
                            auth=False, retry=True)
        res = payload.get("result")
        if not isinstance(res, dict) or not res.get("id") or not res.get("sessionId"):
            raise ClientError("protocol", "iClass 登录响应缺少 id/sessionId")
        self.user_id, self.session_id = str(res["id"]), str(res["sessionId"])

    def login(self, number: str, password: str):
        if not re.fullmatch(r"[A-Za-z0-9]{4,20}", number or ""):
            raise ClientError("input", "学号格式不正确")
        if not password:
            raise ClientError("input", "密码不能为空")
        self.clear()  # 失败时不得沿用上一个账号的身份
        self.trace = []
        try:
            self._gateway_login(number, password)
            self._business_login(number)
        except ClientError as e:
            e.trace = list(self.trace)
            self.clear()
            raise
        except BaseException:
            self.clear()
            raise

    # ---------- 业务接口 ----------
    def _api(self, method: str, logical: str, *, params=None, data=None,
             auth: bool = True, retry: bool = False, empty_status: str = "") -> dict:
        if auth and not self.logged_in:
            raise SessionExpired("尚未登录 iClass")
        headers = {"Sessionid": self.session_id} if auth else {}
        gw = webvpn.gateway_url(logical)
        attempts = 3 if retry else 1
        for i in range(attempts):
            try:
                r = self._send(method, gw, params=params, data=data, headers=headers)
                break
            except ClientError as e:
                if e.kind != "network" or i == attempts - 1:
                    raise
        if 300 <= r.status_code < 400:
            raise SessionExpired()
        ctype = r.headers.get("Content-Type", "")
        body = r.text.lstrip()
        if "html" in ctype.lower() or body[:1] == "<":
            raise SessionExpired("收到登录页/HTML 响应，会话失效或需要额外认证")
        if r.status_code != 200:
            raise ClientError("protocol", f"iClass 接口异常（HTTP {r.status_code}）")
        try:
            payload = r.json()
        except ValueError:
            raise ClientError("protocol", "iClass 响应不是有效 JSON") from None
        if not isinstance(payload, dict):
            raise ClientError("protocol", "iClass 响应结构异常")
        if str(payload.get("STATUS")) != "0" and not (
                empty_status and str(payload.get("STATUS")) == empty_status
                and not payload.get("result")):
            msg = _pick(payload, "ERRORMSG", "errorMsg", "message", default="")
            keys = ",".join(sorted(payload))[:80]
            raise ClientError("business",
                              f"iClass 返回错误（STATUS={payload.get('STATUS')}，字段：{keys}）{msg}")
        return payload

    def schedules(self, day: date) -> list[Lesson]:
        payload = self._api("GET", f"{API}/app/course/get_stu_course_sched.action",
                            params={"dateStr": day.strftime("%Y%m%d"), "id": self.user_id},
                            retry=True, empty_status="2")  # 当天无课时 iClass 返回 STATUS=2 且无 result
        return parse_lessons(payload)

    def _timestamp(self) -> str:
        last: ClientError | None = None
        for base in (API, SIGN_API):  # 只读、幂等，可依次尝试
            try:
                p = self._api("GET", f"{base}/app/common/get_timestamp.action", retry=True)
            except SessionExpired:
                raise
            except ClientError as e:
                last = e
                continue
            res = p.get("result")
            cand = p.get("timestamp", res.get("timestamp") if isinstance(res, dict) else res)
            if cand is not None and re.fullmatch(r"\d{10,16}", str(cand)):
                return str(cand)
            last = ClientError("protocol", "时间戳格式异常")
        raise last or ClientError("protocol", "无法获取时间戳")

    def _find(self, day: date, lesson_id: str) -> Lesson | None:
        return next((x for x in self.schedules(day) if x.id == lesson_id), None)

    def sign(self, day: date, lesson_id: str) -> SignResult:
        # 1. 重新查询最新安排
        lesson = self._find(day, lesson_id)
        if lesson is None:
            raise ClientError("business", "该课程当天已不存在，请刷新课表")
        if lesson.sign_status == 1:
            return SignResult("success", "该课程已签到，无需重复提交")
        if lesson.sign_status != 0:
            raise ClientError("business", "签到状态未知，已拒绝提交；请到 iClass 核实")
        if not lesson.window_open(now_china()):
            raise ClientError("business", "当前不在可签到时间（课前 10 分钟至课程结束）")
        # 2. 学校服务时间戳
        ts = self._timestamp()
        # 3. 再次检查时间，然后只提交一次
        if not lesson.window_open(now_china()):
            raise ClientError("business", "当前不在可签到时间（课前 10 分钟至课程结束）")
        try:
            payload = self._api(
                "POST", f"{SIGN_API}/app/course/stu_scan_sign.action",
                params={"courseSchedId": lesson.id, "timestamp": ts},
                data={"id": self.user_id}, retry=False)
        except ClientError as e:
            if e.kind == "business":
                return SignResult("failed", e.message)
            return self._confirm(day, lesson_id)
        res = payload.get("result")
        if isinstance(res, dict) and str(res.get("stuSignStatus")) == "1":
            return SignResult("success", "签到成功")
        return self._confirm(day, lesson_id)

    def _confirm(self, day: date, lesson_id: str) -> SignResult:
        """签到响应不明确：只回查课表，绝不重放签到 POST。"""
        try:
            again = self._find(day, lesson_id)
        except ClientError:
            again = None
        if again is not None and again.sign_status == 1:
            return SignResult("success", "响应不明确，但回查确认已签到")
        return SignResult("unknown", "无法确认签到结果，请到 iClass 查看记录")
