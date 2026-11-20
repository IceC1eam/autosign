import os
import tempfile
import time
import unittest
from datetime import timedelta

TMP = tempfile.mkdtemp()
os.environ["APP_DATA"] = TMP

from autosign import crypto, db, iclass, notify, scheduler  # noqa: E402
from autosign.iclass import ClientError, Lesson, SignResult  # noqa: E402


class FakeClient:
    lessons: dict = {}      # 课程 id -> Lesson（共享，便于测试修改）
    signs: list = []
    login_error: ClientError | None = None
    captcha_image: tuple | None = (b"\x89PNGfake", "image/png")   # None = 学校当前不要求验证码
    finish_error: ClientError | None = None
    finished: list = []
    sign_result: SignResult | None = None   # 非 None 时 sign() 返回它（如被拒）
    variants: list = []                      # 每次 sign() 用的请求写法

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

    def sign(self, day, lid, variant="eschool"):
        FakeClient.signs.append(lid)
        FakeClient.variants.append(variant)
        if FakeClient.sign_result is not None:     # 模拟学校拒绝等：不改变课程签到状态
            return FakeClient.sign_result
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
        FakeClient.sign_result, FakeClient.variants = None, []
        # 通知：配上假密钥，同步“发送”并只记录，不联网
        os.environ[notify.ENV_KEY] = "SCT" + "a1B2c3D4e5" * 3
        self.sent: list[tuple[str, str]] = []
        self._nd, self._nv = notify._dispatch, notify._deliver
        notify._dispatch = lambda fn, *a: fn(*a)
        notify._deliver = lambda key, title, body, aid: self.sent.append((title, body))
        self._orig = scheduler.Client
        scheduler.Client = FakeClient
        self.m = scheduler.Manager()
        self.aid = db.x("INSERT INTO accounts(label,number,cred,enabled,created_at) VALUES(?,?,?,?,?)",
                        ("t", "20370001", crypto.encrypt("pw"), 1, time.time()))

    def tearDown(self):
        scheduler.Client = self._orig
        notify._dispatch, notify._deliver = self._nd, self._nv
        os.environ.pop(notify.ENV_KEY, None)
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

    def reject_once(self):
        """让到点的计划立刻到期并执行一次（学校拒绝），返回执行后的计划行。"""
        db.x("UPDATE plans SET planned_at=?", (time.time() - 1,))
        self.run_job()
        return self.plan("1")

    def test_retry_delay_dense_before_start_sparse_after(self):
        now = time.time()
        for _ in range(50):
            self.assertTrue(45 <= scheduler.retry_delay(now, now + 300) <= 75)    # 开课前
            self.assertTrue(120 <= scheduler.retry_delay(now, now - 60) <= 180)   # 开课后
        self.assertGreaterEqual(scheduler.MAX_ATTEMPTS, 10)

    def test_rejection_keeps_retrying_beyond_old_five_attempt_limit(self):
        # 回归（2026-11-19）：学校连续拒绝时，旧版 5 次后永久判失败，而窗口实际开到下课
        FakeClient.sign_result = SignResult("failed", "iClass 返回错误（STATUS=1）")
        FakeClient.lessons = {"1": lesson("1", 5)}
        self.run_job()                                  # 建计划
        for n in range(1, 8):
            p = self.reject_once()
            self.assertEqual(p["attempts"], n)
            self.assertEqual(p["status"], "pending", f"第 {n} 次被拒后仍应继续重试")
        self.assertEqual(len(FakeClient.signs), 7)
        # 开课前被拒：下一次约 45–75 秒后
        p = self.plan("1")
        self.assertTrue(40 <= p["planned_at"] - p["updated"] <= 80)

    def test_sign_variant_alternates_after_each_rejection_and_is_logged(self):
        FakeClient.sign_result = SignResult("failed", "iClass 返回错误（STATUS=1，ERRCODE=101）：签到失败！")
        FakeClient.lessons = {"1": lesson("1", 5)}
        self.run_job()
        for _ in range(5):
            self.reject_once()
        self.assertEqual(FakeClient.variants, ["eschool", "legacy", "eschool", "legacy", "eschool"])
        msgs = [r["message"] for r in db.q("SELECT message FROM events WHERE kind='sign' ORDER BY id")]
        self.assertIn("第 1 次，eschool", msgs[0])
        self.assertIn("第 2 次，legacy", msgs[1])

    def test_success_event_names_the_variant_that_worked(self):
        FakeClient.lessons = {"1": lesson("1", 5)}
        self.run_job()
        self.reject_once()
        self.assertEqual(FakeClient.variants, ["eschool"])
        msg = db.one("SELECT message FROM events WHERE kind='sign' ORDER BY id DESC")["message"]
        self.assertIn("签到成功", msg)
        self.assertIn("eschool", msg)

    def test_rejection_gives_up_at_max_attempts(self):
        FakeClient.sign_result = SignResult("failed", "iClass 返回错误（STATUS=1）")
        FakeClient.lessons = {"1": lesson("1", 5)}
        self.run_job()
        for _ in range(scheduler.MAX_ATTEMPTS):
            p = self.reject_once()
        self.assertEqual(p["attempts"], scheduler.MAX_ATTEMPTS)
        self.assertEqual(p["status"], "failed")
        before = len(FakeClient.signs)
        self.run_job(); self.run_job()
        self.assertEqual(len(FakeClient.signs), before)   # 判失败后不再提交

    def test_rejection_after_class_start_backs_off(self):
        FakeClient.sign_result = SignResult("failed", "iClass 返回错误（STATUS=1）")
        FakeClient.lessons = {"1": lesson("1", -3)}     # 已开课 3 分钟，窗口仍开
        self.run_job()
        p = self.reject_once()
        self.assertEqual(p["status"], "pending")
        self.assertTrue(115 <= p["planned_at"] - p["updated"] <= 185)

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

    # ---------- 通知 ----------
    def add_plan(self, lid, start_in_min, name=None, dur=95):
        now = time.time()
        st = now + start_in_min * 60
        db.x("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (self.aid, iclass.now_china().date().isoformat(), lid, name or "课" + lid,
              st, st + dur * 60, st - 300, "pending", 0, "", now))

    def pause(self, ago=3600, state="captcha"):
        db.x("UPDATE accounts SET state=?,state_at=? WHERE id=?", (state, time.time() - ago, self.aid))

    def test_captcha_pause_notifies_once_with_next_lesson(self):
        self.add_plan("1", 120, name="算法设计")
        FakeClient.login_error = ClientError("captcha", "统一认证要求图片验证码")
        self.run_job(force=True)
        self.assertEqual(self.state()["state"], "captcha")
        self.assertEqual(len(self.sent), 1)
        title, body = self.sent[0]
        self.assertIn("验证码", title)
        self.assertIn("账号：t", body)
        self.assertIn("《算法设计》", body)
        self.assertIn("验证码登录", body)
        self.run_job(force=True)            # 再次失败：冷却内不重复
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("20370001", body)   # 不含学号
        self.assertNotIn("pw", body.replace("账号", ""))

    def test_bad_password_pause_notifies(self):
        FakeClient.login_error = ClientError("auth", "学号或密码错误")
        self.run_job(force=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("密码", self.sent[0][1])

    def test_transient_error_does_not_notify(self):
        FakeClient.login_error = ClientError("network", "网络请求失败（ReadTimeout）")
        self.run_job(force=True)
        self.assertEqual(self.state()["state"], "error")
        self.assertEqual(self.sent, [])

    def test_final_failure_notifies_once_with_reason(self):
        FakeClient.sign_result = SignResult("failed", "iClass 返回错误（STATUS=1，ERRCODE=7）：签到未开始")
        FakeClient.lessons = {"1": lesson("1", 5, name="线性系统")}
        self.run_job()
        for _ in range(scheduler.MAX_ATTEMPTS - 1):
            self.reject_once()
        self.assertEqual(self.sent, [], "还有重试机会时不通知")
        self.assertEqual(self.reject_once()["status"], "failed")
        self.assertEqual(len(self.sent), 1)
        title, body = self.sent[0]
        self.assertIn("签到失败", title)
        self.assertIn("《线性系统》", body)
        self.assertIn("签到未开始", body)
        self.assertIn(f"{scheduler.MAX_ATTEMPTS} 次", body)

    def test_same_name_twin_lessons_share_one_failure_notice(self):
        FakeClient.sign_result = SignResult("failed", "拒绝")
        a = lesson("1", 5, name="高等数学", teacher="甲")
        b = lesson("2", 5, name="高等数学", teacher="乙")
        b.start, b.end = a.start, a.end
        FakeClient.lessons = {"1": a, "2": b}
        self.run_job()
        for _ in range(scheduler.MAX_ATTEMPTS):
            db.x("UPDATE plans SET planned_at=?", (time.time() - 1,))
            self.run_job()
        self.assertEqual(self.plan("1")["status"], "failed")
        self.assertEqual(self.plan("2")["status"], "failed")
        self.assertEqual(len(self.sent), 1)

    def test_no_time_left_for_another_try_is_final_failure(self):
        # 下一次尝试已晚于下课：不再停在 pending，直接判失败并通知
        FakeClient.sign_result = SignResult("failed", "拒绝")
        FakeClient.lessons = {"1": lesson("1", -3)}
        self.run_job()
        db.x("UPDATE plans SET planned_at=?,end_ts=?", (time.time() - 1, time.time() + 30))
        self.run_job()
        self.assertEqual(self.plan("1")["status"], "failed")
        self.assertEqual(len(self.sent), 1)

    def test_success_and_unknown_never_notify(self):
        FakeClient.lessons = {"1": lesson("1", 5)}
        self.run_job()
        self.reject_once()
        self.assertEqual(self.plan("1")["status"], "signed")
        FakeClient.lessons = {"2": lesson("2", 5)}
        FakeClient.sign_result = SignResult("unknown", "无法确认")
        self.run_job(force=True)           # 窗口已开：立即提交，学校回“结果未知”
        self.assertEqual(self.plan("2")["status"], "unknown")   # 结果未知由回查机制接管，不算最终失败
        self.assertEqual(self.sent, [])

    def test_paused_account_reminded_when_lesson_is_soon(self):
        self.add_plan("1", 20, name="机器学习")
        self.pause(ago=3600)
        self.m.tick()
        self.assertEqual(len(self.sent), 1)
        title, body = self.sent[0]
        self.assertIn("暂停", title)
        self.assertIn("《机器学习》", body)
        self.m.tick(); self.m.tick()          # 同一节课只提醒一次
        self.assertEqual(len(self.sent), 1)

    def test_paused_reminder_conditions(self):
        self.add_plan("1", 120)               # 还早（> 30 分钟）
        self.pause(ago=3600)
        self.m.tick()
        self.assertEqual(self.sent, [])
        db.x("DELETE FROM plans")
        self.add_plan("2", 20)
        self.pause(ago=60)                    # 刚暂停并通知过：不立刻重复提醒
        self.m.tick()
        self.assertEqual(self.sent, [])
        self.pause(ago=3600, state="ok")      # 不是暂停态：不提醒
        self.m.pool.submit = lambda *a, **k: None
        self.m.tick()
        self.assertEqual(self.sent, [])

    def test_paused_reminder_when_class_already_started(self):
        self.add_plan("1", -10, name="信息系统")
        self.pause(ago=3600)
        self.m.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("已开课", self.sent[0][1])

    def test_no_notification_when_unconfigured(self):
        os.environ.pop(notify.ENV_KEY)
        self.add_plan("1", 20)
        self.pause(ago=3600)
        self.m.tick()
        FakeClient.login_error = ClientError("captcha", "x")
        self.run_job(force=True)
        self.assertEqual(self.sent, [])
        self.assertIsNone(db.one("SELECT 1 FROM notices"))

    def test_notify_exception_never_breaks_scheduling(self):
        def boom(*a):
            raise RuntimeError("push service down")
        notify._deliver = boom
        FakeClient.login_error = ClientError("captcha", "x")
        self.run_job(force=True)              # _dispatch 同步抛出也只被 notify.send 吞掉
        self.assertEqual(self.state()["state"], "captcha")

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
