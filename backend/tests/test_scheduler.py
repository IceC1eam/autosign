import os
import tempfile
import time
import unittest
from datetime import timedelta

TMP = tempfile.mkdtemp()
os.environ["APP_DATA"] = TMP

from autosign import crypto, db, iclass, scheduler  # noqa: E402
from autosign.iclass import ClientError, Lesson, SignResult  # noqa: E402


class FakeClient:
    lessons: dict = {}      # 课程 id -> Lesson（共享，便于测试修改）
    signs: list = []
    login_error: ClientError | None = None
    captcha_image: tuple | None = (b"\x89PNGfake", "image/png")   # None = 学校当前不要求验证码
    finish_error: ClientError | None = None
    finished: list = []

    def __init__(self):
        self._in = False

    @property
    def logged_in(self):
        return self._in

    def clear(self):
        self._in = False

    def login(self, number, password):
        if FakeClient.login_error:
            raise FakeClient.login_error
        self._in = True

    def begin_captcha_login(self):
        return FakeClient.captcha_image

    def finish_captcha_login(self, number, password, code):
        FakeClient.finished.append((number, password, code))
        if FakeClient.finish_error:
            raise FakeClient.finish_error
        self._in = True

    def schedules(self, day):
        return list(FakeClient.lessons.values())

    def sign(self, day, lid):
        FakeClient.signs.append(lid)
        FakeClient.lessons[lid].sign_status = 1
        return SignResult("success", "签到成功")


def lesson(lid, start_in_min, dur=95, signed=0, name=None, teacher="师"):
    now = iclass.now_china()
    s = now + timedelta(minutes=start_in_min)
    return Lesson(lid, name or "课" + lid, teacher, "室", s, s + timedelta(minutes=dur), signed)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        db.reset_for_tests()
        for f in os.listdir(TMP):
            os.remove(os.path.join(TMP, f))
        FakeClient.lessons, FakeClient.signs, FakeClient.login_error = {}, [], None
        FakeClient.captcha_image, FakeClient.finish_error, FakeClient.finished = (b"\x89PNGfake", "image/png"), None, []
        self._orig = scheduler.Client
        scheduler.Client = FakeClient
        self.m = scheduler.Manager()
        self.aid = db.x("INSERT INTO accounts(label,number,cred,enabled,created_at) VALUES(?,?,?,?,?)",
                        ("t", "20370001", crypto.encrypt("pw"), 1, time.time()))

    def tearDown(self):
        scheduler.Client = self._orig
        self.m.shutdown()

    def run_job(self, force=False):
        self.m.runner(self.aid).running = True
        self.m._job(self.aid, force)

    def plan(self, lid):
        return db.one("SELECT * FROM plans WHERE account_id=? AND lesson_id=?", (self.aid, lid))

    def test_planned_time_inside_ten_minute_window(self):
        FakeClient.lessons = {"1": lesson("1", 60)}
        self.run_job()
        p = self.plan("1")
        self.assertEqual(p["status"], "pending")
        lo, hi = p["start_ts"] - 600, p["start_ts"]
        self.assertTrue(lo <= p["planned_at"] <= hi, (p["planned_at"] - lo))
        self.assertEqual(FakeClient.signs, [])

    def test_signs_when_planned_time_reached_once(self):
        FakeClient.lessons = {"1": lesson("1", 5)}   # 已在窗口内
        self.run_job()
        db.x("UPDATE plans SET planned_at=?", (time.time() - 1,))
        self.run_job()
        self.assertEqual(FakeClient.signs, ["1"])
        self.assertEqual(self.plan("1")["status"], "signed")
        self.run_job(); self.run_job()
        self.assertEqual(FakeClient.signs, ["1"])

    def test_enable_signs_immediately_if_window_open(self):
        FakeClient.lessons = {"1": lesson("1", 4), "2": lesson("2", 120), "3": lesson("3", -30)}
        self.run_job(force=True)
        self.assertIn("1", FakeClient.signs)
        self.assertIn("3", FakeClient.signs)     # 正在上课且未签
        self.assertNotIn("2", FakeClient.signs)  # 还没到窗口
        self.assertEqual(self.plan("2")["status"], "pending")

    def test_already_signed_not_resigned(self):
        FakeClient.lessons = {"1": lesson("1", 3, signed=1)}
        self.run_job(force=True)
        self.assertEqual(FakeClient.signs, [])
        self.assertEqual(self.plan("1")["status"], "signed")

    def test_disabled_account_does_nothing(self):
        db.x("UPDATE accounts SET enabled=0")
        FakeClient.lessons = {"1": lesson("1", 3)}
        self.run_job(force=True)
        self.assertEqual(FakeClient.signs, [])

    def test_bad_password_pauses_account(self):
        FakeClient.login_error = ClientError("auth", "学号或密码错误")
        FakeClient.lessons = {"1": lesson("1", 3)}
        self.run_job(force=True)
        self.assertEqual(db.one("SELECT state FROM accounts")["state"], "auth_failed")
        # tick 不会再去重试已暂停的账号
        self.m.pool.submit = lambda *a, **k: self.fail("paused account must not be retried")
        self.m.tick()

    def test_same_name_same_time_shares_planned_time(self):
        # 同名同时段、教师不同：共用同一个随机时刻；同时段不同名、同名不同时段则各自随机
        base = lesson("1", 120, name="高等数学", teacher="甲")
        twin = lesson("2", 120, name="高等数学", teacher="乙")
        twin.start, twin.end = base.start, base.end
        other = lesson("3", 120, name="大学物理")
        other.start, other.end = base.start, base.end
        later = lesson("4", 300, name="高等数学")
        FakeClient.lessons = {"1": base, "2": twin, "3": other, "4": later}
        for _ in range(3):
            self.run_job()
        p1, p2, p3, p4 = (self.plan(i) for i in "1234")
        self.assertEqual(p1["planned_at"], p2["planned_at"])
        self.assertNotEqual(p1["planned_at"], p3["planned_at"])
        self.assertNotEqual(p1["planned_at"], p4["planned_at"])

    def test_late_sibling_reuses_existing_planned_time(self):
        first = lesson("1", 120, name="高等数学", teacher="甲")
        FakeClient.lessons = {"1": first}
        self.run_job()
        twin = lesson("2", 120, name="高等数学", teacher="乙")
        twin.start, twin.end = first.start, first.end
        FakeClient.lessons["2"] = twin
        self.run_job(force=True)   # 课表中途多出同名同时段的班级
        self.assertEqual(self.plan("1")["planned_at"], self.plan("2")["planned_at"])

    def test_ended_lesson_not_planned(self):
        FakeClient.lessons = {"1": lesson("1", -200)}
        self.run_job(force=True)
        self.assertIsNone(self.plan("1"))
        self.assertEqual(FakeClient.signs, [])

    # ---------- 人工输入验证码登录 ----------
    def state(self):
        return db.one("SELECT state,state_msg FROM accounts WHERE id=?", (self.aid,))

    def test_captcha_login_hands_session_to_runner(self):
        db.x("UPDATE accounts SET state='captcha' WHERE id=?", (self.aid,))
        FakeClient.lessons = {"1": lesson("1", 60)}
        begin = self.m.captcha_begin(self.aid)
        self.assertTrue(begin["required"])
        self.assertTrue(begin["image"].startswith("data:image/png;base64,"))
        self.m.pool.submit = lambda *a, **k: None   # 不真正起后台任务，只检查会话交接
        self.m.captcha_finish(self.aid, "ab12")
        self.assertEqual(FakeClient.finished, [("20370001", "pw", "ab12")])   # 用已存密码解密后提交
        self.assertTrue(self.m.runner(self.aid).client.logged_in)
        self.assertEqual(self.state()["state"], "ok")
        self.assertTrue(db.one("SELECT 1 FROM events WHERE kind='login'"))

    def test_captcha_token_is_single_use(self):
        self.m.captcha_begin(self.aid)
        self.m.pool.submit = lambda *a, **k: None
        self.m.captcha_finish(self.aid, "ab12")
        with self.assertRaises(ClientError) as cm:   # 表单 token 只能用一次
            self.m.captcha_finish(self.aid, "ab12")
        self.assertEqual(cm.exception.kind, "input")
        self.assertEqual(len(FakeClient.finished), 1)

    def test_captcha_rejected_keeps_paused_and_consumes_token(self):
        FakeClient.finish_error = ClientError("auth", "学号、密码或验证码有误")
        self.m.captcha_begin(self.aid)
        with self.assertRaises(ClientError):
            self.m.captcha_finish(self.aid, "zzzz")
        # 提交了验证码仍被拒，不能断定是密码错：保持“需要验证码”，不标 auth_failed
        self.assertEqual(self.state()["state"], "captcha")
        self.assertFalse(self.m.runner(self.aid).client.logged_in)
        with self.assertRaises(ClientError) as cm:
            self.m.captcha_finish(self.aid, "zzzz")
        self.assertEqual(cm.exception.kind, "input")
        self.assertEqual(len(FakeClient.finished), 1)

    def test_captcha_finish_expired(self):
        self.m.captcha_begin(self.aid)
        c, ts = self.m._pending[self.aid]
        self.m._pending[self.aid] = (c, ts - scheduler.CAPTCHA_TTL - 1)
        with self.assertRaises(ClientError):
            self.m.captcha_finish(self.aid, "ab12")
        self.assertEqual(FakeClient.finished, [])

    def test_captcha_begin_rate_limited(self):
        self.m.captcha_begin(self.aid)
        with self.assertRaises(ClientError):
            self.m.captcha_begin(self.aid)

    def test_captcha_not_required_returns_flag(self):
        FakeClient.captcha_image = None
        self.assertEqual(self.m.captcha_begin(self.aid), {"required": False})

    def test_forget_drops_pending_login(self):
        self.m.captcha_begin(self.aid)
        self.m.forget(self.aid)
        self.assertNotIn(self.aid, self.m._pending)

    def test_scheduler_pauses_on_captcha_without_retry(self):
        FakeClient.login_error = ClientError("captcha", iclass.CAPTCHA_MSG)
        FakeClient.lessons = {"1": lesson("1", 3)}
        self.run_job(force=True)
        self.assertEqual(self.state()["state"], "captcha")
        self.m.pool.submit = lambda *a, **k: self.fail("captcha-paused account must not be retried")
        self.m.tick()


if __name__ == "__main__":
    unittest.main()
