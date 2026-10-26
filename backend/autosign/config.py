"""运行环境：只读环境变量，由平台（systemd 单元）注入。"""
from __future__ import annotations

import os
from pathlib import Path

APP_ID = "autosign"
MAX_ACCOUNTS = 20


def data_dir() -> Path:
    """应用私有数据目录（APP_DATA，平台为每个应用单独创建，权限 700）。"""
    d = Path(os.environ.get("APP_DATA", "./data")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def frontend_dir() -> Path:
    default = Path(__file__).resolve().parents[2] / "frontend"
    return Path(os.environ.get("APP_FRONTEND", default))
