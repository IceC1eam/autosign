"""HTTP 接口：账号管理、课表、手动签到、动态。

不做站点登录/CSRF：访问控制由平台网关（nginx auth_request → portal）统一负责。
"""
from __future__ import annotations

import re
import time
from datetime import date as Date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import config, crypto, db
from .audit import audit
from .iclass import now_china
from .scheduler import manager

router = APIRouter(prefix="/api")

NUMBER_RE = re.compile(r"[A-Za-z0-9]{4,20}")


class AccountIn(BaseModel):
    label: str = Field(default="", max_length=40)
    number: str = Field(max_length=32)
    password: str = Field(max_length=128)
    enabled: bool = False


class AccountPatch(BaseModel):
    label: str | None = Field(default=None, max_length=40)
    number: str | None = Field(default=None, max_length=32)
    password: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None


class SignReq(BaseModel):
    date: Date
    id: str = Field(max_length=64)


def _plans_today(aid: int) -> list[dict]:
    rows = db.q("SELECT * FROM plans WHERE account_id=? AND day=? ORDER BY start_ts",
                (aid, now_china().date().isoformat()))
    return [{"id": r["lesson_id"], "name": r["name"], "start": r["start_ts"], "end": r["end_ts"],
             "planned_at": r["planned_at"], "status": r["status"], "attempts": r["attempts"],
             "message": r["message"]} for r in rows]


def _account_dict(r) -> dict:
    return {"id": r["id"], "label": r["label"] or r["number"], "number": r["number"],
            "enabled": bool(r["enabled"]), "state": r["state"], "state_msg": r["state_msg"],
            "state_at": r["state_at"], "plans": _plans_today(r["id"])}


def _get(aid: int):
    r = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
    if not r:
        raise HTTPException(404, "账号不存在")
    return r


def _check_number(number: str) -> str:
    number = number.strip()
    if not NUMBER_RE.fullmatch(number):
        raise HTTPException(400, "学号格式不正确（4–20 位字母或数字）")
    return number


# ---------------- 账号 ----------------
@router.get("/accounts")
def list_accounts():
    return {"now": time.time(), "accounts": [_account_dict(r) for r in db.q("SELECT * FROM accounts ORDER BY id")]}


@router.post("/accounts")
def add_account(body: AccountIn):
    number = _check_number(body.number)
    if not body.password:
        raise HTTPException(400, "密码不能为空")
    if db.one("SELECT COUNT(*) c FROM accounts")["c"] >= config.MAX_ACCOUNTS:
        raise HTTPException(400, f"最多管理 {config.MAX_ACCOUNTS} 个账号")
    if db.one("SELECT 1 FROM accounts WHERE number=?", (number,)):
        raise HTTPException(409, "该学号已存在")
    aid = db.x("INSERT INTO accounts(label,number,cred,enabled,created_at) VALUES(?,?,?,?,?)",
               (body.label.strip(), number, crypto.encrypt(body.password), int(body.enabled), time.time()))
    audit(f"account add id={aid} number={number} enabled={body.enabled}")
    db.event(aid, "account", "账号已添加" + ("并开启" if body.enabled else ""))
    if body.enabled:
        manager.kick(aid, force=True)
    return _account_dict(_get(aid))


@router.patch("/accounts/{aid}")
def patch_account(aid: int, body: AccountPatch):
    acc = _get(aid)
    changed_cred = False
    if body.label is not None:
        db.x("UPDATE accounts SET label=? WHERE id=?", (body.label.strip(), aid))
    if body.number is not None:
        number = _check_number(body.number)
        if number != acc["number"]:
            if db.one("SELECT 1 FROM accounts WHERE number=? AND id<>?", (number, aid)):
                raise HTTPException(409, "该学号已存在")
            db.x("UPDATE accounts SET number=? WHERE id=?", (number, aid))
            changed_cred = True
    if body.password:
        db.x("UPDATE accounts SET cred=? WHERE id=?", (crypto.encrypt(body.password), aid))
        changed_cred = True
    if changed_cred:
        manager.forget(aid)
        db.x("UPDATE accounts SET state='idle',state_msg='' WHERE id=?", (aid,))
        db.event(aid, "account", "账号信息已更新")
    if body.enabled is not None and bool(acc["enabled"]) != body.enabled:
        db.x("UPDATE accounts SET enabled=?,state='idle',state_msg='' WHERE id=?", (int(body.enabled), aid))
        db.event(aid, "account", "已开启自动签到" if body.enabled else "已关闭自动签到")
        audit(f"account toggle id={aid} enabled={body.enabled}")
        if body.enabled:
            manager.kick(aid, force=True)  # 立即检查并签到当前可签的课程
    elif changed_cred and db.one("SELECT enabled FROM accounts WHERE id=?", (aid,))["enabled"]:
        manager.kick(aid, force=True)
    return _account_dict(_get(aid))


@router.delete("/accounts/{aid}")
def delete_account(aid: int):
    _get(aid)
    manager.forget(aid)
    db.x("DELETE FROM plans WHERE account_id=?", (aid,))
    db.x("DELETE FROM events WHERE account_id=?", (aid,))
    db.x("DELETE FROM accounts WHERE id=?", (aid,))
    audit(f"account delete id={aid}")
    return {"ok": True}


@router.post("/accounts/{aid}/check")
def check_account(aid: int):
    acc = _get(aid)
    if not acc["enabled"]:
        raise HTTPException(400, "账号未开启")
    manager.kick(aid, force=True)
    return {"ok": True}


# ---------------- 课表 / 手动签到 ----------------
@router.get("/accounts/{aid}/schedule")
def schedule(aid: int, date: Date | None = None):
    _get(aid)
    day = date or now_china().date()
    lessons = manager.schedule(aid, day)
    now = now_china()
    return {"date": day.isoformat(), "lessons": [x.to_dict(now) for x in lessons]}


@router.post("/accounts/{aid}/sign")
def sign(aid: int, body: SignReq):
    _get(aid)
    audit(f"manual sign REQUEST account={aid} date={body.date} id={body.id}")
    res = manager.manual_sign(aid, body.date, body.id)
    audit(f"manual sign RESULT account={aid} id={body.id} status={res.status}")
    return {"status": res.status, "message": res.message}


# ---------------- 动态 ----------------
@router.get("/events")
def events(limit: int = 80, account: int | None = None):
    limit = max(1, min(limit, 300))
    if account is not None:
        rows = db.q("SELECT * FROM events WHERE account_id=? ORDER BY id DESC LIMIT ?", (account, limit))
    else:
        rows = db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    labels = {r["id"]: (r["label"] or r["number"]) for r in db.q("SELECT id,label,number FROM accounts")}
    return {"events": [{"ts": r["ts"], "account_id": r["account_id"], "account": labels.get(r["account_id"], ""),
                        "kind": r["kind"], "message": r["message"]} for r in rows]}
