"""应用操作审计日志（账号增删/开关、手动签到），不含密码与令牌。"""
from __future__ import annotations

import os

from . import config
from .iclass import now_china


def audit(msg: str):
    line = f"{now_china().isoformat(timespec='seconds')} {msg}\n"
    fd = os.open(config.data_dir() / "audit.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line)
