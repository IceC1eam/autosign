"""通知（Server酱）：账号需要人工处理、或某节课最终签到失败时推送到微信。

约束：
- 通知绝不能影响签到：异步发送、短超时、任何异常只记日志；没配置密钥就什么都不做。
- 密钥只来自环境变量 NOTIFY_SERVERCHAN_KEY（`scripts/deploy.sh secret autosign` 经 stdin 装入，不进仓库）。
  请求异常的文本里带完整 URL（含密钥），所以只记录异常类型，不记录异常文本。
- 通知内容只含账号标签、课程名、时间和学校返回的原因；不含学号、密码、会话信息、URL。
- 去重/冷却由 db.claim_notice 保证，落库，重启后仍有效。
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from . import db

log = logging.getLogger("autosign.notify")

ENV_KEY = "NOTIFY_SERVERCHAN_KEY"
TIMEOUT = 5                                   # 秒
_KEY_RE = re.compile(r"[A-Za-z0-9]{10,100}")  # 进 URL 路径，必须是纯字母数字
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")


def _key() -> str:
    k = os.environ.get(ENV_KEY, "").strip()
    return k if _KEY_RE.fullmatch(k) else ""


def configured() -> bool:
    return bool(_key())


def _endpoint(key: str) -> str:
    # Server酱³（sctp<uid>t...）与 Server酱 Turbo（SCT...）的地址不同
    m = re.fullmatch(r"sctp(\d+)t[A-Za-z0-9]+", key)
    if m:
        return f"https://{m.group(1)}.push.ft07.com/send/{key}.send"
    return f"https://sctapi.ftqq.com/{key}.send"


def _deliver(key: str, title: str, body: str, account_id: int | None) -> None:
    try:
        r = requests.post(_endpoint(key), data={"title": title[:32], "desp": body},
                          timeout=TIMEOUT, allow_redirects=False)
        ok, what = r.status_code == 200, f"HTTP {r.status_code}"
        if ok:
            try:
                d = r.json()
                ok = not isinstance(d, dict) or d.get("code", 0) == 0
                if not ok:
                    what = f"接口返回 code={d.get('code')}"
            except ValueError:
                pass
    except requests.RequestException as e:
        ok, what = False, type(e).__name__    # 不记录异常文本：里面有带密钥的 URL
    except Exception as e:                    # noqa: BLE001 —— 通知失败不能冒泡
        ok, what = False, type(e).__name__
    if not ok:
        log.warning("serverchan push failed: %s", what)
        try:
            db.event(account_id, "notify", f"通知发送失败（{what}）")
        except Exception:                     # noqa: BLE001
            log.exception("record notify failure")


def _dispatch(fn, *args) -> None:
    _pool.submit(fn, *args)                   # 测试里替换成同步调用


def send(dedupe_key: str, title: str, body: str, *, account_id: int | None = None,
         cooldown: float = 7200) -> bool:
    """发一条通知；同一 dedupe_key 在 cooldown 秒内只发一次。返回是否真的安排了发送。

    未配置密钥时不认领 dedupe_key（之后配上密钥，当前仍成立的事件还能补发）。
    """
    try:
        key = _key()
        if not key or not db.claim_notice(dedupe_key, cooldown):
            return False
        _dispatch(_deliver, key, title, body, account_id)
        return True
    except Exception:                         # noqa: BLE001 —— 绝不影响调用方（调度/签到）
        log.exception("notify failed")
        return False
