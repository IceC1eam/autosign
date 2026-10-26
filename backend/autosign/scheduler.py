"""多账号自动签到调度。

规则：
- 账号处于“开”时，在每节未签到课程开始前 10 分钟的窗口内随机挑一个时刻签到；
  同一时间、同名的课程（只是教师不同）取同一个随机时刻；
- 账号由“关”切到“开”时，立即检查并签到当前已可签、且未签到的课程；
- 签到 POST 只发一次（Client.sign 内部保证），响应不明确时只回查，不重放；
- 账号密码错误/需要验证码时暂停该账号，避免反复失败登录触发学校风控。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from . import crypto, db
from .iclass import CHINA, Client, ClientError, SessionExpired, now_china

log = logging.getLogger("autosign.scheduler")

WINDOW_LEAD = 600          # 课前 10 分钟
TICK = 5                   # 调度循环间隔（秒）
MAX_ATTEMPTS = 5
PAUSED_STATES = ("auth_failed", "captcha")


def hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, CHINA).strftime("%H:%M:%S")


class Runner:
    """单个账号的运行时：独立的 iClass 会话与锁。"""

    def __init__(self, account_id: int):
        self.id = account_id
        self.client = Client()
        self.lock = threading.RLock()
        self.running = False
        self.next_poll = 0.0


class Manager:
    def __init__(self):
        self.runners: dict[int, Runner] = {}
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="sign")
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None
        self._guard = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop_ev.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def runner(self, aid: int) -> Runner:
        with self._guard:
            return self.runners.setdefault(aid, Runner(aid))

    def forget(self, aid: int):
        with self._guard:
            r = self.runners.pop(aid, None)
        if r:
            with r.lock:
                r.client.clear()

    def _loop(self):
        while not self.stop_ev.wait(TICK):
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")

    def tick(self):
        now = time.time()
        for acc in db.q("SELECT id,state FROM accounts WHERE enabled=1"):
            if acc["state"] in PAUSED_STATES:
                continue
            r = self.runner(acc["id"])
            if r.running:
                continue
            due = db.one("SELECT 1 FROM plans WHERE account_id=? AND status='pending' "
                         "AND planned_at<=? AND end_ts>=?", (acc["id"], now, now))
            if now >= r.next_poll or due:
                self.kick(acc["id"])

    def kick(self, aid: int, force: bool = False):
        r = self.runner(aid)
        with self._guard:
            if r.running and not force:
                return
            r.running = True
        self.pool.submit(self._job, aid, force)

    # ---------- 任务 ----------
    def _job(self, aid: int, force: bool):
        r = self.runner(aid)
        try:
            with r.lock:
                acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
                if not acc or not acc["enabled"]:
                    return
                if force:
                    db.x("UPDATE accounts SET state='idle',state_msg='' WHERE id=? AND state IN "
                         "('auth_failed','captcha','error')", (aid,))
                    acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
                if force or time.time() >= r.next_poll:
                    self._poll(acc, r, force)
                self._attempt_due(acc, r)
        except ClientError as e:
            self._on_error(aid, r, e)
        except Exception:
            log.exception("job failed for account %s", aid)
            db.event(aid, "error", "内部错误，详见服务日志")
            r.next_poll = time.time() + 300
        finally:
            r.running = False

    def _on_error(self, aid: int, r: Runner, e: ClientError):
        now = time.time()
        if e.kind in ("auth", "captcha"):
            state = "auth_failed" if e.kind == "auth" else "captcha"
            db.x("UPDATE accounts SET state=?,state_msg=?,state_at=? WHERE id=?",
                 (state, e.message, now, aid))
            db.event(aid, "error", f"登录失败，已暂停该账号：{e.message}")
        else:
            db.x("UPDATE accounts SET state='error',state_msg=?,state_at=? WHERE id=?",
                 (e.message, now, aid))
            db.event(aid, "error", f"{e.kind}: {e.message}")
            r.next_poll = now + 300
            if e.trace:
                db.event(aid, "trace", " | ".join(e.trace[-6:]))

    # ---------- 登录与调用 ----------
    def _ensure_login(self, acc, r: Runner):
        if r.client.logged_in:
            return
        number, password = acc["number"], crypto.decrypt(acc["cred"])
        r.client.login(number, password)
        db.x("UPDATE accounts SET state='ok',state_msg='',state_at=? WHERE id=?", (time.time(), acc["id"]))
        db.event(acc["id"], "login", "已登录 iClass")

    def call(self, acc, r: Runner, fn, read_only: bool):
        with r.lock:
            self._ensure_login(acc, r)
            try:
                return fn(r.client)
            except SessionExpired:
                r.client.clear()
                if not read_only:
                    raise
                self._ensure_login(acc, r)
                return fn(r.client)

    # ---------- 轮询课表 ----------
    def _poll(self, acc, r: Runner, force: bool):
        aid = acc["id"]
        now_dt = now_china()
        today = now_dt.date()
        lessons = self.call(acc, r, lambda c: c.schedules(today), True)
        now = time.time()
        day = today.isoformat()
        pending = signed = 0
        slot_time: dict[tuple[str, float], float] = {}   # 同名同时段 -> 共用的计划时刻
        for L in lessons:
            st, en = L.start.timestamp(), L.end.timestamp()
            row = db.one("SELECT * FROM plans WHERE account_id=? AND day=? AND lesson_id=?",
                         (aid, day, L.id))
            if L.sign_status == 1:
                signed += 1
                if row is None:
                    db.x("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (aid, day, L.id, L.name, st, en, st, "signed", 0, "已签到", now))
                elif row["status"] != "signed":
                    db.x("UPDATE plans SET status='signed',message=?,updated=? WHERE account_id=? AND day=? AND lesson_id=?",
                         ("课表显示已签到", now, aid, day, L.id))
                    db.event(aid, "sign", f"《{L.name}》确认已签到")
                continue
            if L.sign_status is None:
                if row is None:
                    db.x("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (aid, day, L.id, L.name, st, en, st, "unknown", 0, "签到状态未知，不自动提交", now))
                continue
            # 未签到
            if en < now:
                continue  # 课程已结束，不再安排
            pending += 1
            if row is None or (row["status"] == "pending" and abs(row["start_ts"] - st) > 1):
                planned = self._slot_time(aid, day, L, now, slot_time)
                db.x("INSERT OR REPLACE INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (aid, day, L.id, L.name, st, en, planned, "pending", 0,
                      f"计划 {hms(planned)} 签到", now))
                db.event(aid, "plan", f"《{L.name}》计划于 {hms(planned)} 签到")
            elif row["status"] in ("signed", "unknown") and row["attempts"] < MAX_ATTEMPTS \
                    and now - row["updated"] > 90:
                # 回查确认仍为未签到：安排再试一次（此前的提交没有生效）
                db.x("UPDATE plans SET status='pending',planned_at=?,message=?,updated=? "
                     "WHERE account_id=? AND day=? AND lesson_id=?",
                     (now + 5 + random.random() * 25, "回查仍未签到，重新安排", now, aid, day, L.id))
            # 切换为“开”时：当前已进入可签窗口的课程立即签到
            if force and db.one("SELECT 1 FROM plans WHERE account_id=? AND day=? AND lesson_id=? "
                                "AND status='pending'", (aid, day, L.id)) \
                    and st - WINDOW_LEAD <= now <= en:
                db.x("UPDATE plans SET planned_at=?,message=? WHERE account_id=? AND day=? AND lesson_id=?",
                     (now, "账号刚开启，立即签到", aid, day, L.id))
                db.event(aid, "plan", f"《{L.name}》已在可签窗口，立即签到")
        # 下次轮询时间
        nxt = db.one("SELECT MIN(planned_at) m, MIN(start_ts) s FROM plans WHERE account_id=? "
                     "AND status='pending' AND end_ts>=?", (aid, now))
        if nxt and nxt["s"] is not None:
            soon = nxt["s"] - WINDOW_LEAD - now
            r.next_poll = now + (120 if soon < 900 else max(60, min(1200, soon - 600)))
        else:
            r.next_poll = now + 1800
        db.x("UPDATE accounts SET state='ok',state_msg=?,state_at=? WHERE id=?",
             (f"{hms(now)} 已检查：今日 {len(lessons)} 节，已签 {signed}，待签 {pending}", now, aid))

    @staticmethod
    def _slot_time(aid: int, day: str, L, now: float, slot_time: dict) -> float:
        """同一时间、同名的课程只是教师不同，取同一个随机时刻：先复用已有计划，再复用本轮已定的，最后才新随机。"""
        if L.slot not in slot_time:
            st = L.start.timestamp()
            for r in db.q("SELECT name,planned_at FROM plans WHERE account_id=? AND day=? AND status='pending' "
                          "AND attempts=0 AND ABS(start_ts-?)<=1 AND lesson_id<>? ORDER BY planned_at",
                          (aid, day, st, L.id)):
                if " ".join(r["name"].split()) == L.slot[0]:
                    slot_time[L.slot] = max(r["planned_at"], now + 1)
                    break
            else:
                slot_time[L.slot] = max(st - WINDOW_LEAD + 2 + random.random() * (WINDOW_LEAD - 3),
                                        now + 1 + random.random() * 19)
        return slot_time[L.slot]

    # ---------- 执行到点的签到 ----------
    def _attempt_due(self, acc, r: Runner):
        aid = acc["id"]
        while True:
            now = time.time()
            plan = db.one("SELECT * FROM plans WHERE account_id=? AND status='pending' "
                          "AND planned_at<=? AND end_ts>=? ORDER BY planned_at LIMIT 1", (aid, now, now))
            if not plan:
                return
            self._attempt(acc, r, plan)

    def _attempt(self, acc, r: Runner, plan):
        aid, key = acc["id"], (plan["account_id"], plan["day"], plan["lesson_id"])
        n = plan["attempts"] + 1
        # 先落库再提交：即使进程中途退出也不会无限重复
        db.x("UPDATE plans SET attempts=?,message=?,updated=? WHERE account_id=? AND day=? AND lesson_id=?",
             (n, "提交中", time.time(), *key))
        day = date.fromisoformat(plan["day"])
        try:
            res = self.call(acc, r, lambda c: c.sign(day, plan["lesson_id"]), False)
        except ClientError as e:
            if e.kind in ("auth", "captcha"):
                db.x("UPDATE plans SET status='pending',message=?,updated=? WHERE account_id=? AND day=? AND lesson_id=?",
                     (e.message, time.time(), *key))
                raise
            retry = n < MAX_ATTEMPTS and time.time() < plan["end_ts"]
            db.x("UPDATE plans SET status=?,planned_at=?,message=?,updated=? WHERE account_id=? AND day=? AND lesson_id=?",
                 ("pending" if retry else "failed", time.time() + 45 + random.random() * 30,
                  e.message, time.time(), *key))
            db.event(aid, "sign", f"《{plan['name']}》未能提交（第 {n} 次）：{e.message}")
            return
        now = time.time()
        if res.status == "success":
            db.x("UPDATE plans SET status='signed',message=?,updated=? WHERE account_id=? AND day=? AND lesson_id=?",
                 (res.message, now, *key))
            db.event(aid, "sign", f"《{plan['name']}》签到成功（{hms(now)}）")
        elif res.status == "failed":
            retry = n < MAX_ATTEMPTS and now < plan["end_ts"]
            db.x("UPDATE plans SET status=?,planned_at=?,message=?,updated=? WHERE account_id=? AND day=? AND lesson_id=?",
                 ("pending" if retry else "failed", now + 45 + random.random() * 30, res.message, now, *key))
            db.event(aid, "sign", f"《{plan['name']}》签到被拒（第 {n} 次）：{res.message}")
        else:
            db.x("UPDATE plans SET status='unknown',message=?,updated=? WHERE account_id=? AND day=? AND lesson_id=?",
                 (res.message, now, *key))
            db.event(aid, "sign", f"《{plan['name']}》结果未知：{res.message}")

    # ---------- 手动 ----------
    def manual_sign(self, aid: int, day: date, lesson_id: str):
        acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc:
            raise ClientError("input", "账号不存在")
        r = self.runner(aid)
        try:
            res = self.call(acc, r, lambda c: c.sign(day, lesson_id), False)
        except ClientError as e:
            db.event(aid, "sign", f"手动签到未提交：{e.message}")
            raise
        if res.status == "success":
            db.x("UPDATE plans SET status='signed',message=?,updated=? WHERE account_id=? AND day=? AND lesson_id=?",
                 ("手动签到成功", time.time(), aid, day.isoformat(), lesson_id))
        db.event(aid, "sign", f"手动签到：{res.status} {res.message}")
        return res

    def week(self, aid: int, days: list[date]):
        """整周课表：沿用已有会话依次查询每天（只读），返回 [(日期, 课程列表)]。"""
        acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc:
            raise ClientError("input", "账号不存在")
        return self.call(acc, self.runner(aid), lambda c: [(d, c.schedules(d)) for d in days], True)

    def schedule(self, aid: int, day: date):
        acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc:
            raise ClientError("input", "账号不存在")
        return self.call(acc, self.runner(aid), lambda c: c.schedules(day), True)


manager = Manager()
