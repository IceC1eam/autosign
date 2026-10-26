"""北航账号密码的落库加密（Fernet，密钥文件 600，位于本应用数据目录）。"""
from __future__ import annotations

import os
from pathlib import Path

from . import config


def write_private(path: Path, data: bytes):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _fernet():
    from cryptography.fernet import Fernet
    kf = config.data_dir() / "secret.key"
    if not kf.exists():
        write_private(kf, Fernet.generate_key())
    return Fernet(kf.read_bytes())


def encrypt(text: str) -> bytes:
    return _fernet().encrypt(text.encode())


def decrypt(blob: bytes) -> str:
    return _fernet().decrypt(blob).decode()
