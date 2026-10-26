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

    def schedules(self, day):
        return list(FakeClient.lessons.values())

    def sign(self, day, lid):
        FakeClient.signs.append(lid)
        FakeClient.lessons[lid].sign_status = 1
        return SignResult("success", "签到成功")


def lesson(lid, start_in_min, dur=95, signed=0):
    now = iclass.now_china()
    s = now + timedelta(minutes=start_in_min)
    return Lesson(lid, "课" + lid, "师", "室", s, s + timedelta(minutes=dur), signed)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        db.reset_for_tests()
        for f in os.listdir(TMP):
            os.remove(os.path.join(TMP, f))
        FakeClient.lessons, FakeClient.signs, FakeClient.login_error = {}, [], None
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

    def test_ended_lesson_not_planned(self):
        FakeClient.lessons = {"1": lesson("1", -200)}
        self.run_job(force=True)
        self.assertIsNone(self.plan("1"))
        self.assertEqual(FakeClient.signs, [])


if __name__ == "__main__":
    unittest.main()
