import os
import tempfile
import unittest

TMP = tempfile.mkdtemp()
os.environ["APP_DATA"] = TMP

from fastapi.testclient import TestClient  # noqa: E402

from autosign import db  # noqa: E402
from autosign.main import app  # noqa: E402


class ApiTests(unittest.TestCase):
    """应用本身不做登录（网关负责），接口直接可用；这里只验证契约。"""

    def setUp(self):
        db.reset_for_tests()
        for f in os.listdir(TMP):
            os.remove(os.path.join(TMP, f))
        self.c = TestClient(app)

    def test_healthz(self):
        self.assertEqual(self.c.get("/healthz").json(), {"ok": True, "app": "autosign"})

    def test_account_crud_without_login(self):
        r = self.c.post("/api/accounts", json={"label": "t", "number": "20370001", "password": "pw"})
        self.assertEqual(r.status_code, 200, r.text)
        aid = r.json()["id"]
        self.assertNotIn("password", r.text)
        self.assertEqual(self.c.post("/api/accounts", json={"number": "20370001", "password": "x"}).status_code, 409)
        self.assertEqual(self.c.post("/api/accounts", json={"number": "a b", "password": "x"}).status_code, 400)
        self.assertEqual(len(self.c.get("/api/accounts").json()["accounts"]), 1)
        self.assertEqual(self.c.delete(f"/api/accounts/{aid}").status_code, 200)
        self.assertEqual(self.c.get("/api/accounts").json()["accounts"], [])

    def test_error_shape(self):
        r = self.c.get("/api/accounts/999/schedule")
        self.assertEqual((r.status_code, r.json()["error"]["kind"]), (404, "http"))

    def test_week_schedule(self):
        from datetime import date, datetime, timedelta

        from autosign import iclass
        from autosign.scheduler import manager

        aid = self.c.post("/api/accounts", json={"number": "20370001", "password": "pw"}).json()["id"]
        t = datetime(2026, 10, 21, 8, 0, tzinfo=iclass.CHINA)   # 周三
        mk = lambda lid, teacher, st: iclass.Lesson(lid, "高等数学", teacher, "主楼", t, t + timedelta(minutes=95), st)
        seen = []

        def fake_week(_aid, days):
            seen.extend(days)
            return [(d, [mk("a", "甲", 1), mk("b", "乙", 0)] if d == date(2026, 10, 21) else []) for d in days]

        orig, manager.week = manager.week, fake_week
        try:
            r = self.c.get(f"/api/accounts/{aid}/week?date=2026-10-23")   # 周五 -> 所在周周一为 10-19
        finally:
            manager.week = orig
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual((j["start"], j["end"]), ("2026-10-19", "2026-10-25"))
        self.assertEqual(len(seen), 7)
        wed = j["days"][2]
        self.assertEqual(wed["date"], "2026-10-21")
        self.assertEqual([x["same_slot"] for x in wed["lessons"]], [2, 2])
        self.assertEqual(j["summary"], {"total": 2, "signed": 1, "unsigned": 1, "unknown": 0})

    def test_frontend_uses_relative_api_paths(self):
        # 应用挂在 /<id>/ 下，前端若用绝对 /api/... 会打到入口而不是本应用
        from pathlib import Path
        js = (Path(__file__).resolve().parents[2] / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn(r'path.replace(/^\//, "")', js)


if __name__ == "__main__":
    unittest.main()
