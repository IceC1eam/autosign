"""autosign 应用入口：只负责组装（生命周期、安全头、错误格式、静态前端）。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .api import router
from .iclass import ClientError
from .scheduler import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    manager.start()
    yield
    manager.shutdown()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.include_router(router)


@app.get("/healthz")
def healthz():
    """平台健康检查（portal 直连本机端口探测）。"""
    return {"ok": True, "app": config.APP_ID}


@app.middleware("http")
async def headers_mw(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return resp


@app.exception_handler(ClientError)
async def client_error_handler(request: Request, exc: ClientError):
    status = {"session": 409, "input": 400, "auth": 422, "captcha": 422,
              "network": 502, "business": 400, "protocol": 502}.get(exc.kind, 500)
    err = {"kind": exc.kind, "message": exc.message}
    if exc.trace:
        err["trace"] = exc.trace[-12:]
    return JSONResponse({"error": err}, status_code=status)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return JSONResponse({"error": {"kind": "http", "message": exc.detail}},
                        status_code=exc.status_code, headers=exc.headers)


_frontend = config.frontend_dir()
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="static")
