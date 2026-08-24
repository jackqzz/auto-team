"""FastAPI 主程序：路由 + SSE 流式日志。

启动:
    python -m webui.app
或者:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import queue
import secrets
import sys
import time
import uuid
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import db, export_formats, proxy_usage, registrar  # noqa: E402
from . import workspace_membership  # noqa: E402
from . import public_relogin  # noqa: E402
from .auto_loop import (  # noqa: E402
    CONTROLLER as AUTO_LOOP,
    LOGIN_CONTROLLER,
    all_login_controllers,
    login_controller_for,
)
from .exporter import _decode_jwt_payload, _get_auth  # noqa: E402
from mail_providers import (  # noqa: E402
    ImportValidationError,
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_pooled_providers,
    list_providers,
)

# 启动时自动释放卡死的 in_use 号（上次进程崩溃 / 强退留下的）
try:
    _released = db.release_stale_in_use(stale_seconds=1800)
    if _released > 0:
        logging.getLogger("webui").info(f"[startup] 释放 {_released} 个卡死的 in_use 号")
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] release_stale 失败: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")

_TASK_LOG_LOCK = threading.Lock()
_TASK_LOG_SEQ = 0
_TASK_LOGS: deque[dict] = deque(maxlen=3000)
_TASK_LOGGER_NAMES = {"webui", "workspace_membership", "auto_loop", "registrar"}
_WORKSPACE_DB_ID_RE = re.compile(r"\bworkspace_db_id=(\d+)\b")
_WORKSPACE_ID_RE = re.compile(r"\bworkspace_id=([0-9a-fA-F-]{8,})\b")
_WORKSPACE_ALIAS_RE = re.compile(r"\bworkspace=(\d+)\b")


def _push_task_log(record: logging.LogRecord) -> None:
    global _TASK_LOG_SEQ
    if record.name not in _TASK_LOGGER_NAMES:
        return
    try:
        text = record.getMessage()
    except Exception:
        text = str(record.msg)
    if not any(token in text for token in ("workspace_db_id=", "workspace_id=", "workspace=", "候选", "席位", "额度查询", "垃圾箱", "申请加入", "邀请", "校验")):
        return
    entry = {
        "id": 0,
        "ts": time.time(),
        "logger": record.name,
        "level": record.levelname,
        "text": text,
        "workspace_db_id": None,
        "workspace_external_id": None,
    }
    m = _WORKSPACE_DB_ID_RE.search(text) or _WORKSPACE_ALIAS_RE.search(text)
    if m:
        try:
            entry["workspace_db_id"] = int(m.group(1))
        except Exception:
            pass
    m = _WORKSPACE_ID_RE.search(text)
    if m:
        entry["workspace_external_id"] = m.group(1)
    with _TASK_LOG_LOCK:
        _TASK_LOG_SEQ += 1
        entry["id"] = _TASK_LOG_SEQ
        _TASK_LOGS.append(entry)


class _TaskLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _push_task_log(record)
        except Exception:
            pass


_task_log_handler = _TaskLogHandler()
for _logger_name in _TASK_LOGGER_NAMES:
    logging.getLogger(_logger_name).addHandler(_task_log_handler)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="GPT Outlook Register WebUI", docs_url=None, redoc_url=None)

_INVITE_STATUS_RECHECK_DELAY_SECONDS = 5
_ADMIN_SESSIONS: dict[str, float] = {}
_ADMIN_SESSION_TTL_SECONDS = 24 * 3600


def _admin_password_hash() -> str:
    env_password = str(os.getenv("WEBUI_ADMIN_PASSWORD", "") or "").strip()
    if env_password:
        return hashlib.sha256(env_password.encode("utf-8")).hexdigest()
    return str(db.get_admin_auth_config().get("admin_password_hash") or "").strip()


def _admin_auth_enabled() -> bool:
    return bool(_admin_password_hash())


def _issue_admin_token() -> str:
    token = secrets.token_urlsafe(32)
    _ADMIN_SESSIONS[token] = time.time() + _ADMIN_SESSION_TTL_SECONDS
    return token


def _cleanup_admin_sessions() -> None:
    now = time.time()
    expired = [token for token, expires_at in _ADMIN_SESSIONS.items() if expires_at <= now]
    for token in expired:
        _ADMIN_SESSIONS.pop(token, None)


def _current_admin_token(request: Request) -> str:
    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header_token = str(request.headers.get("x-admin-token") or "").strip()
    if header_token:
        return header_token
    return str(request.cookies.get("gpt_auto_register_admin_token") or "").strip()


def _is_public_api_path(path: str) -> bool:
    return path.startswith("/api/auth") or path.startswith("/api/public-relogin")


@app.middleware("http")
async def _admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not _is_public_api_path(path) and _admin_auth_enabled():
        _cleanup_admin_sessions()
        token = _current_admin_token(request)
        if not token or _ADMIN_SESSIONS.get(token, 0) <= time.time():
            return JSONResponse({"detail": "需要管理员登录"}, status_code=401)
        _ADMIN_SESSIONS[token] = time.time() + _ADMIN_SESSION_TTL_SECONDS
    return await call_next(request)


# ──────────────────────── Pydantic 模型 ────────────────────────


class ImportReq(BaseModel):
    text: str = Field(..., description="每行一个号，格式由 kind 决定")
    kind: str = Field(
        "",
        description="邮箱来源（outlook / ...）。留空则按段数猜，"
                    "但 Outlook 和 Gmail 都是 4 段，猜不出来，建议前端必填",
    )
    group_name: Optional[str] = Field(
        None,
        description="导入到指定分组；空字符串=未分组，未提供=保留已有账号原分组",
        max_length=64,
    )


class Sub2APIImportReq(BaseModel):
    text: str = Field(..., description="Sub2API JSON 文件内容")
    group_name: str = Field("", description="导入账号分组")


class WorkspaceSessionImportReq(BaseModel):
    text: str = Field(..., description="母号 Session；支持 account----session、纯 session 或 JSON")
    proxy: str = Field("", description="本批母号的专属代理；每行/JSON 内代理可覆盖")


class WorkspaceProxyReq(BaseModel):
    proxy: str = Field(..., description="母号专属代理")


class WorkspaceBulkDeleteReq(BaseModel):
    ids: list[int] = Field(..., min_length=1, description="要删除的母号记录 ID")


class WorkspaceCandidatesReq(BaseModel):
    workspace_id: int
    emails: list[str] = Field(default_factory=list)
    proxy: str = ""
    proxy_pool: str = ""
    quota_proxy: str = Field(
        "",
        description="前端在本次手动额度任务快照中预选的全局池代理",
    )
    seat_type: str = "default"
    tag_status: str = ""
    auto_push: bool = False
    relogin_on_401: bool = False
    concurrency: int = Field(1, ge=1, le=20)
    otp_timeout: int = Field(180, ge=10, le=600)
    account_retry_count: int = Field(1, ge=1, le=5)
    cool_down_seconds: int = Field(0, ge=0, le=3600)
    trash_enabled: bool = True
    trash_invalid_enabled: bool = True
    trash_zero_delay_minutes: int = Field(60, ge=1, le=1440)


class WorkspaceCandidateInviteStatusReq(BaseModel):
    workspace_id: int
    emails: list[str] = Field(default_factory=list)
    join_status: str = Field(..., description="not_invited / pending_invite / joined")


class WorkspaceQuotaScheduleReq(BaseModel):
    workspace_id: int
    interval_minutes: int = Field(30, ge=5, le=1440)
    relogin_on_401: bool = False
    proxy_pool: str = ""
    auto_push: bool = False
    concurrency: int = Field(1, ge=1, le=20)
    otp_timeout: int = Field(180, ge=10, le=600)
    account_retry_count: int = Field(1, ge=1, le=5)
    cool_down_seconds: int = Field(0, ge=0, le=3600)
    trash_enabled: bool = True
    trash_invalid_enabled: bool = True
    trash_zero_delay_minutes: int = Field(60, ge=1, le=1440)
    seat_protect_enabled: bool = False
    seat_protect_threshold: int = Field(8, ge=1, le=1000)
    seat_protect_refresh_time: str = Field("00:00", description="席位保护阈值刷新时间（HH:MM，CST）")
    auto_standard_seat_enabled: bool = False


class WorkspaceAutoSeatReq(BaseModel):
    workspace_id: int


class AdminLoginReq(BaseModel):
    password: str = Field(..., min_length=1)


class PublicReloginSettingsReq(BaseModel):
    public_relogin_enabled: bool = False
    proxy_pool: str = ""
    use_system_proxy_pool: bool = True
    concurrency: int = Field(3, ge=1, le=20)
    quota_queue_capacity: int = Field(512, ge=1, le=10000)
    relogin_queue_capacity: int = Field(128, ge=1, le=10000)
    retry_count: int = Field(2, ge=0, le=5)
    quota_timeout: int = Field(30, ge=5, le=120)
    login_timeout: int = Field(180, ge=30, le=900)
    admin_password: str = Field("", description="留空不修改；传入新值则启用/更新管理端鉴权")
    clear_admin_password: bool = Field(False, description="清空管理员密码并关闭管理端鉴权")


class PublicReloginAccessKeyCreateReq(BaseModel):
    name: str = Field("", max_length=80)
    expires_in_days: Optional[int] = Field(3, description="有效天数；0 表示永久", ge=0, le=3650)


class PublicReloginCheckReq(BaseModel):
    accounts: list[dict] = Field(default_factory=list)
    access_key: str = ""
    proxy_pool: str = ""
    concurrency: int = Field(0, ge=0, le=20)
    auto_relogin_on_401: bool = False


class PublicReloginReq(BaseModel):
    accounts: list[dict] = Field(default_factory=list)
    access_key: str = ""
    proxy_pool: str = ""
    concurrency: int = Field(0, ge=0, le=20)


_QuotaScheduler = tuple[threading.Event, threading.Thread, int, bool, float]
_quota_schedulers_lock = threading.Lock()
_quota_schedulers: dict[int, _QuotaScheduler] = {}
_seat_auto_schedulers_lock = threading.Lock()
_seat_auto_schedulers: dict[int, tuple[threading.Event, threading.Thread, float]] = {}
_workspace_member_sync_lock = threading.Lock()
_workspace_member_sync_running: set[int] = set()


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    token = _current_admin_token(request)
    authenticated = (not _admin_auth_enabled()) or (_ADMIN_SESSIONS.get(token, 0) > time.time())
    return {"ok": True, "enabled": _admin_auth_enabled(), "authenticated": authenticated}


@app.post("/api/auth/login")
def api_auth_login(req: AdminLoginReq):
    expected = _admin_password_hash()
    if not expected:
        return {"ok": True, "token": _issue_admin_token(), "auth_disabled": True}
    actual = hashlib.sha256(str(req.password or "").encode("utf-8")).hexdigest()
    if not secrets.compare_digest(actual, expected):
        raise HTTPException(401, "管理员密码错误")
    return {"ok": True, "token": _issue_admin_token()}


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    token = _current_admin_token(request)
    if token:
        _ADMIN_SESSIONS.pop(token, None)
    return {"ok": True}


def _public_relogin_proxy_pool(req_pool: str, configured_pool: str) -> list[str]:
    values = list(dict.fromkeys(
        line.strip()
        for line in str(req_pool or configured_pool or "").replace(",", "\n").splitlines()
        if line.strip()
    ))
    return values


def _public_relogin_effective_proxy_pool(req_pool: str, cfg: dict) -> list[str]:
    """公开重登代理池。

    优先级：
    1. 前端本次请求传入的代理池（用于复用系统代理池）；
    2. 后台公开重登独立代理池；
    3. 无代理池，单账号 proxy 仍在调用处优先使用。
    """
    configured_pool = cfg.get("proxy_pool") or ""
    use_system_pool = bool(cfg.get("use_system_proxy_pool", True))
    return _public_relogin_proxy_pool(req_pool if use_system_pool else "", configured_pool)


def _public_relogin_account_key(account: dict, index: int) -> str:
    if str(account.get("id") or "").strip():
        return str(account.get("id")).strip()
    try:
        normalized = public_relogin.normalized_account(account)
        return normalized.get("email") or f"row-{index + 1}"
    except Exception:
        return f"row-{index + 1}"


def _validate_public_relogin_access_key(access_key: str) -> dict:
    key = db.validate_public_relogin_access_key(access_key)
    if not key:
        raise HTTPException(403, "公开重登访问密钥无效或已过期")
    return key


def _public_relogin_validate(account: dict, cfg: dict) -> tuple[dict | None, dict | None]:
    try:
        normalized = public_relogin.normalized_account(account)
    except Exception as exc:
        return None, {"ok": False, "status": "invalid", "error": f"账号格式无效: {exc}"}
    return normalized, None


def _run_public_relogin_account(
    account: dict,
    normalized: dict,
    cfg: dict,
    proxy_leases: public_relogin.ProxyLeasePool,
    *,
    initial_exclude_proxy: str = "",
) -> dict:
    """执行单账号公开重登；巡检和手动重登共用同一套代理轮换规则。"""
    account_proxy = str(account.get("proxy") or "").strip()
    previous_proxy = initial_exclude_proxy
    last_error = ""
    for attempt in range(1, cfg["retry_count"] + 2):
        if account_proxy:
            proxy = account_proxy
        else:
            proxy, proxy_index, lease_count = proxy_leases.lease(
                previous_proxy,
                task_type="login",
                task_detail="public_401_relogin",
            )
            if proxy:
                logger.info(
                    "公开401重登领取代理 account=%s attempt=%s pool_index=%s leased_count=%s",
                    normalized.get("email", ""), attempt, proxy_index + 1, lease_count,
                )

        def switch_proxy(current_proxy: str, reason: str) -> str:
            if account_proxy:
                return account_proxy
            replacement, replacement_index, replacement_count = proxy_leases.lease(
                current_proxy,
                task_type="login",
                task_detail="public_401_relogin",
            )
            if replacement:
                logger.warning(
                    "公开401重登切换代理 account=%s attempt=%s pool_index=%s "
                    "leased_count=%s reason=%s",
                    normalized.get("email", ""), attempt, replacement_index + 1,
                    replacement_count, reason,
                )
            return replacement

        try:
            refreshed = public_relogin.relogin_account(
                normalized or account,
                proxy=proxy,
                login_timeout=cfg["login_timeout"],
                on_proxy_switch=switch_proxy,
            )
            return {
                "ok": True,
                "status": "revived",
                "attempt": attempt,
                "email": refreshed.get("email", ""),
                "workspace_id": refreshed.get("chatgpt_account_id", ""),
                "account": refreshed,
            }
        except Exception as exc:
            last_error = str(exc)
            previous_proxy = proxy
            if public_relogin._looks_deactivated(last_error):
                return {
                    "ok": False,
                    "status": "deactivated",
                    "attempt": attempt,
                    "email": normalized.get("email", ""),
                    "workspace_id": normalized.get("chatgpt_account_id", ""),
                    "error": last_error[:500],
                }
            if attempt <= cfg["retry_count"]:
                time.sleep(2)
    return {
        "ok": False,
        "status": "failed",
        "attempt": cfg["retry_count"] + 1,
        "email": normalized.get("email", ""),
        "workspace_id": normalized.get("chatgpt_account_id", ""),
        "error": last_error[:500],
    }


@app.get("/api/settings/public-relogin")
def api_get_public_relogin_settings():
    cfg = db.get_public_relogin_config()
    public_relogin.configure_task_dispatchers(public_relogin.get_effective_config())
    auth_cfg = db.get_admin_auth_config()
    cfg["use_system_proxy_pool"] = str(cfg.get("use_system_proxy_pool") or "1").lower() in {"1", "true", "yes", "on"}
    return {
        "ok": True,
        "config": {
            **cfg,
            "auth_enabled": bool(auth_cfg.get("admin_password_hash") or os.getenv("WEBUI_ADMIN_PASSWORD")),
            "admin_password": "",
            "access_keys": db.list_public_relogin_access_keys(),
        },
    }


@app.post("/api/settings/public-relogin")
def api_save_public_relogin_settings(req: PublicReloginSettingsReq):
    payload = req.model_dump()
    payload["use_system_proxy_pool"] = "1" if req.use_system_proxy_pool else "0"
    db.save_public_relogin_config(payload)
    if req.clear_admin_password:
        db.save_admin_auth_config({"admin_password": ""})
        _ADMIN_SESSIONS.clear()
    elif req.admin_password.strip():
        db.save_admin_auth_config({"admin_password": req.admin_password.strip()})
        _ADMIN_SESSIONS.clear()
    cfg = db.get_public_relogin_config()
    public_relogin.configure_task_dispatchers(public_relogin.get_effective_config())
    return {
        "ok": True,
        "config": {
            **cfg,
            "auth_enabled": _admin_auth_enabled(),
            "admin_password": "",
            "access_keys": db.list_public_relogin_access_keys(),
        },
    }


@app.post("/api/settings/public-relogin/access-keys")
def api_create_public_relogin_access_key(req: PublicReloginAccessKeyCreateReq):
    now = time.time()
    expires_at = 0 if int(req.expires_in_days or 0) == 0 else now + int(req.expires_in_days or 3) * 86400
    key = db.create_public_relogin_access_key(req.name, expires_at)
    return {"ok": True, "access_key": key, "access_keys": db.list_public_relogin_access_keys()}


@app.delete("/api/settings/public-relogin/access-keys/{key_id}")
def api_revoke_public_relogin_access_key(key_id: str):
    if not db.revoke_public_relogin_access_key(key_id):
        raise HTTPException(404, "访问密钥不存在")
    return {"ok": True, "access_keys": db.list_public_relogin_access_keys()}


@app.post("/api/public-relogin/check")
async def api_public_relogin_check(req: PublicReloginCheckReq):
    cfg = public_relogin.get_effective_config()
    if not cfg["enabled"]:
        raise HTTPException(403, "公开 401 重登录页面未启用")
    _validate_public_relogin_access_key(req.access_key)
    public_relogin.configure_task_dispatchers(cfg)
    accounts = req.accounts or []
    if not accounts:
        raise HTTPException(400, "请先导入账号")
    if len(accounts) > 500:
        raise HTTPException(400, "单次最多检查 500 个账号")
    proxies = _public_relogin_effective_proxy_pool(req.proxy_pool, cfg)
    proxy_leases = public_relogin.ProxyLeasePool(proxies)
    results: dict[str, dict] = {}

    def check_one(index: int, account: dict) -> tuple[str, dict]:
        key = _public_relogin_account_key(account, index)
        normalized, error = _public_relogin_validate(account, cfg)
        if error:
            return key, error
        proxy = str(account.get("proxy") or "").strip()
        if not proxy:
            proxy, _, _ = proxy_leases.lease(
                task_type="quota",
                task_detail="public_quota",
            )
        try:
            quota = public_relogin.fetch_quota(normalized or account, proxy=proxy, timeout=cfg["quota_timeout"])
            return key, {"ok": True, "status": "active", "email": normalized.get("email", ""), "workspace_id": normalized.get("chatgpt_account_id", ""), "quota": quota}
        except public_relogin.PublicQuotaUnauthorized as exc:
            if req.auto_relogin_on_401:
                logger.warning(
                    "公开定时巡检发现401，立即启动重登 account=%s",
                    normalized.get("email", ""),
                )
                try:
                    relogin_future = public_relogin.RELOGIN_TASKS.submit(
                        lambda: _run_public_relogin_account(
                            account,
                            normalized,
                            cfg,
                            proxy_leases,
                            initial_exclude_proxy=proxy,
                        )
                    )
                except public_relogin.PublicTaskQueueFull as queue_error:
                    return key, {
                        "ok": False,
                        "status": "queue_full",
                        "detected_401": True,
                        "email": normalized.get("email", ""),
                        "workspace_id": normalized.get("chatgpt_account_id", ""),
                        "error": str(queue_error),
                    }
                # 额度 worker 到这里立即返回并释放执行槽；HTTP 请求线程在下方
                # 等待 relogin_future，不占用额度查询并发。
                return key, {"_relogin_future": relogin_future}
            return key, {"ok": False, "status": "401", "email": normalized.get("email", ""), "workspace_id": normalized.get("chatgpt_account_id", ""), "error": str(exc)}
        except public_relogin.PublicAccountDeactivated as exc:
            return key, {"ok": False, "status": "deactivated", "email": normalized.get("email", ""), "workspace_id": normalized.get("chatgpt_account_id", ""), "error": str(exc)}
        except Exception as exc:
            return key, {"ok": False, "status": "error", "email": normalized.get("email", ""), "workspace_id": normalized.get("chatgpt_account_id", ""), "error": str(exc)[:500]}

    tasks = [
        (lambda idx=idx, account=account: check_one(idx, account))
        for idx, account in enumerate(accounts)
    ]
    try:
        futures = public_relogin.QUOTA_TASKS.submit_many(tasks)
    except public_relogin.PublicTaskQueueFull as exc:
        raise HTTPException(429, str(exc))
    completed = await asyncio.gather(*(asyncio.wrap_future(future) for future in futures))
    for key, value in completed:
        relogin_future = value.pop("_relogin_future", None)
        if relogin_future is not None:
            value = await asyncio.wrap_future(relogin_future)
            value["detected_401"] = True
        results[key] = value
    return {
        "ok": True,
        "results": results,
        "queues": public_relogin.task_queue_status(),
    }


@app.get("/api/public-relogin/queue-status")
def api_public_relogin_queue_status():
    cfg = public_relogin.get_effective_config()
    if not cfg["enabled"]:
        raise HTTPException(403, "公开 401 重登录页面未启用")
    public_relogin.configure_task_dispatchers(cfg)
    return {"ok": True, "queues": public_relogin.task_queue_status()}


@app.post("/api/public-relogin/relogin")
async def api_public_relogin_relogin(req: PublicReloginReq):
    cfg = public_relogin.get_effective_config()
    if not cfg["enabled"]:
        raise HTTPException(403, "公开 401 重登录页面未启用")
    _validate_public_relogin_access_key(req.access_key)
    public_relogin.configure_task_dispatchers(cfg)
    accounts = req.accounts or []
    if not accounts:
        raise HTTPException(400, "请选择要重新登录的账号")
    if len(accounts) > 200:
        raise HTTPException(400, "单次最多重登 200 个账号")
    proxies = _public_relogin_effective_proxy_pool(req.proxy_pool, cfg)
    proxy_leases = public_relogin.ProxyLeasePool(proxies)
    results: dict[str, dict] = {}

    def relogin_one(index: int, account: dict) -> tuple[str, dict]:
        key = _public_relogin_account_key(account, index)
        normalized, error = _public_relogin_validate(account, cfg)
        if error:
            return key, error
        return key, _run_public_relogin_account(account, normalized, cfg, proxy_leases)

    tasks = [
        (lambda idx=idx, account=account: relogin_one(idx, account))
        for idx, account in enumerate(accounts)
    ]
    try:
        futures = public_relogin.RELOGIN_TASKS.submit_many(tasks)
    except public_relogin.PublicTaskQueueFull as exc:
        raise HTTPException(429, str(exc))
    completed = await asyncio.gather(*(asyncio.wrap_future(future) for future in futures))
    for key, value in completed:
        results[key] = value
    return {
        "ok": True,
        "results": results,
        "concurrency": cfg["concurrency"],
        "proxy_pool_usage": proxy_leases.snapshot(),
        "queues": public_relogin.task_queue_status(),
    }

def _is_codex_seat(value: object) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized in {"usage_based", "usagebased", "codex", "codex席位"}

def _workspace_settings_snapshot(workspace_id: int, overrides: dict | None = None) -> dict:
    cfg = dict(db.get_workspace_settings(workspace_id))
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if k != "workspace_id"})
    return cfg


def _proxy_pool_values(value: object) -> list[str]:
    return list(dict.fromkeys(
        line.strip()
        for line in str(value or "").splitlines()
        if line.strip()
    ))


def _candidate_quota_proxy_pool(
    proxy_pool: object,
    *,
    preferred_proxy: str = "",
) -> public_relogin.ProxyLeasePool:
    values = _proxy_pool_values(proxy_pool)
    if not values:
        raise ValueError("全局代理池为空，候选额度查询无法租取代理")
    preferred = str(preferred_proxy or "").strip()
    if preferred:
        if preferred not in values:
            raise ValueError("额度任务预选代理不属于当前全局代理池")
        values = [preferred, *(proxy for proxy in values if proxy != preferred)]
    return public_relogin.ProxyLeasePool(values)


def _lease_candidate_quota_proxy(
    leases: public_relogin.ProxyLeasePool,
    *,
    workspace_id: int,
    email: str,
    detail: str,
    exclude_proxy: str = "",
) -> str:
    proxy, index, leased_count = leases.lease(
        exclude_proxy,
        task_type="quota",
        task_detail=detail,
    )
    if not proxy:
        raise ValueError("全局代理池为空，候选额度查询无法租取代理")
    logging.getLogger("workspace_membership").info(
        "候选额度查询领取全局代理 workspace=%s email=%s pool_index=%s leased_count=%s detail=%s",
        workspace_id,
        email,
        index + 1,
        leased_count,
        detail,
    )
    return proxy

def _is_zero_quota_payload(payload: dict) -> bool:
    if not isinstance(payload, dict) or payload.get("error_code"):
        return False
    credits = payload.get("credits_balance")
    try:
        if credits is not None and float(credits) <= 0:
            return True
    except Exception:
        pass
    for key in ("primary", "secondary"):
        window = payload.get(key) or {}
        used = window.get("used_percent")
        try:
            if used is not None and float(used) >= 100:
                return True
        except Exception:
            pass
    return False

def _workspace_login_options(workspace_id: int, email: str, settings: dict, *, auto_export: bool = False) -> dict:
    master = db.get_workspace_master(workspace_id)
    if not master or not master.get("workspace_id"):
        raise HTTPException(400, "母号缺少 Workspace ID")
    proxy_pool = str(settings.get("proxy_pool") or "").strip()
    if not proxy_pool:
        raise HTTPException(400, "全局代理池为空")
    return {
        "login_only": True,
        # 空间凭证任务只刷新目标 Workspace 的 token，不应因为账号本地缺少
        # 密码/TOTP 而修改账号安全设置；公开/批量仅登录页面会显式开启它。
        "ensure_credentials": False,
        "login_emails": [str(email).strip().lower()],
        "group_name": "__all__",
        "workspace_id": master.get("workspace_id", ""),
        "workspace_db_id": workspace_id,
        "proxy_pool": proxy_pool,
        "proxy": "",
        "proxy_usage_detail": "workspace_401_relogin",
        "concurrency": int(settings.get("concurrency", 1) or 1),
        "otp_timeout": int(settings.get("otp_timeout", 180) or 180),
        "want_access_token": True,
        "want_session_token": True,
        "want_refresh_token": True,
        "want_password": False,
        "want_2fa": False,
        "allow_existing_login": True,
        "cool_down_seconds": int(settings.get("cool_down_seconds", 0) or 0),
        "account_retry_count": int(settings.get("account_retry_count", 1) or 1),
        "auto_export": bool(auto_export or settings.get("auto_push")),
        "export_refresh_oauth": False,
        "target_count": 0,
    }

def _wait_for_login_completion(
    workspace_id: int,
    email: str,
    timeout: int = 1800,
    *,
    ensure_credentials: bool = False,
) -> bool:
    controller = login_controller_for(
        workspace_db_id=workspace_id,
        workspace_id=(db.get_workspace_master(workspace_id) or {}).get("workspace_id", ""),
        ensure_credentials=ensure_credentials,
    )
    deadline = time.time() + max(30, int(timeout))
    key = str(email or "").strip().lower()
    while time.time() < deadline:
        try:
            with controller._lock:  # noqa: SLF001 - 仅限内部等待逻辑
                pending = any((row.get("email") or "").strip().lower() == key for row in controller._login_queue)
                running = any((info.get("email") or "").strip().lower() == key for info in controller._worker_status.values())
        except Exception:
            pending = running = False
        if not pending and not running:
            return True
        time.sleep(1)
    return False

def _quota_worker(workspace_id: int, interval: int, stop: threading.Event, relogin_on_401: bool, proxy_pool: str, auto_push: bool, concurrency: int, otp_timeout: int, account_retry_count: int, cool_down_seconds: int):
    while not stop.is_set():
        settings = _workspace_settings_snapshot(workspace_id)
        trash_delay = _candidate_trash_delay_seconds(workspace_id, settings)
        candidates = [
            row for row in db.list_workspace_candidate_options(workspace_id)
            if (
                row.get("has_workspace_access_token")
                and row.get("account_status") != "permanently_invalid"
                and not _is_codex_seat(row.get("seat_type"))
            )
        ]
        quota_leases = None
        if candidates:
            try:
                quota_leases = _candidate_quota_proxy_pool(
                    settings.get("proxy_pool"),
                )
            except ValueError as exc:
                logging.getLogger("workspace_membership").error(
                    "定时额度查询批次跳过 workspace=%s count=%s error=%s",
                    workspace_id,
                    len(candidates),
                    exc,
                )
                stop.wait(interval * 60)
                continue
        configured_concurrency = settings.get("concurrency", concurrency)
        worker_count = min(
            max(1, int(configured_concurrency or 1)),
            20,
            max(1, len(candidates)),
        )
        cursor = 0
        cursor_lock = threading.Lock()
        quota_logger = logging.getLogger("workspace_membership")
        quota_logger.info(
            "定时额度查询批次开始 workspace=%s count=%s concurrency=%s",
            workspace_id,
            len(candidates),
            worker_count,
        )

        def process_row(row: dict) -> None:
            email = row.get("email") or ""
            quota_proxy = ""
            try:
                quota_proxy = _lease_candidate_quota_proxy(
                    quota_leases,
                    workspace_id=workspace_id,
                    email=email,
                    detail="workspace_quota_scheduled",
                )
                quota = workspace_membership.fetch_candidate_quota(
                    workspace_id,
                    email,
                    proxy=quota_proxy,
                )
            except workspace_membership.QuotaUnauthorized:
                quota_logger.warning(
                    "定时额度查询 401 workspace=%s email=%s",
                    workspace_id,
                    email,
                    exc_info=True,
                )
                if bool(settings.get("relogin_on_401", relogin_on_401)):
                    try:
                        if _wait_and_relogin_for_candidate(
                            workspace_id,
                            email,
                            settings,
                            auto_export=bool(settings.get("auto_push", auto_push)),
                        ):
                            retry_proxy = _lease_candidate_quota_proxy(
                                quota_leases,
                                workspace_id=workspace_id,
                                email=email,
                                detail="workspace_quota_scheduled",
                                exclude_proxy=quota_proxy,
                            )
                            quota = workspace_membership.fetch_candidate_quota(
                                workspace_id,
                                email,
                                proxy=retry_proxy,
                            )
                        else:
                            return
                    except Exception:
                        quota_logger.warning(
                            "定时额度 401 重登录后复查失败 workspace=%s email=%s",
                            workspace_id,
                            email,
                            exc_info=True,
                        )
                        return
                else:
                    return
            except Exception:
                quota_logger.warning(
                    "定时额度查询失败 workspace=%s email=%s",
                    workspace_id,
                    email,
                    exc_info=True,
                )
                return
            if _is_zero_quota_payload(quota) and _candidate_trash_enabled(workspace_id, settings):
                _schedule_candidate_trash(
                    workspace_id,
                    email,
                    reason="quota_zero",
                    delay_seconds=trash_delay,
                )

        def rolling_worker() -> None:
            nonlocal cursor
            while not stop.is_set():
                with cursor_lock:
                    if cursor >= len(candidates):
                        return
                    row = candidates[cursor]
                    cursor += 1
                process_row(row)

        if candidates:
            # 只创建固定数量的 worker；每个 worker 完成当前账号后才领取下一个。
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(rolling_worker) for _ in range(worker_count)]
                for future in futures:
                    try:
                        future.result()
                    except Exception:
                        quota_logger.exception("定时额度查询 worker 异常 workspace=%s", workspace_id)
        stop.wait(interval * 60)


def _quota_schedule_request_from_settings(
    workspace_id: int,
    settings: dict,
) -> WorkspaceQuotaScheduleReq:
    """Validate persisted settings before using them to create a worker."""
    return WorkspaceQuotaScheduleReq.model_validate(
        {**dict(settings or {}), "workspace_id": int(workspace_id)}
    )


def _start_quota_scheduler(
    workspace_id: int,
    settings: dict,
    *,
    replace: bool = False,
    source: str = "runtime",
) -> tuple[_QuotaScheduler, bool]:
    """Start one scheduler per workspace and return ``(item, started)``."""
    req = _quota_schedule_request_from_settings(workspace_id, settings)
    workspace_id = int(workspace_id)
    with _quota_schedulers_lock:
        current = _quota_schedulers.get(workspace_id)
        if current and current[1].is_alive() and not replace:
            return current, False
        if current:
            current[0].set()

        stop = threading.Event()
        thread = threading.Thread(
            target=_quota_worker,
            args=(
                workspace_id,
                req.interval_minutes,
                stop,
                req.relogin_on_401,
                req.proxy_pool,
                req.auto_push,
                req.concurrency,
                req.otp_timeout,
                req.account_retry_count,
                req.cool_down_seconds,
            ),
            daemon=True,
            name=f"quota-scheduler-{workspace_id}",
        )
        next_at = time.time() + req.interval_minutes * 60
        item: _QuotaScheduler = (
            stop,
            thread,
            req.interval_minutes,
            req.relogin_on_401,
            next_at,
        )
        _quota_schedulers[workspace_id] = item
        try:
            thread.start()
        except Exception:
            if _quota_schedulers.get(workspace_id) is item:
                _quota_schedulers.pop(workspace_id, None)
            raise

    logger.info(
        "定时额度查询任务启动 workspace_db_id=%s interval_minutes=%s source=%s",
        workspace_id,
        req.interval_minutes,
        source,
    )
    return item, True


def _stop_quota_scheduler(workspace_id: int) -> _QuotaScheduler | None:
    with _quota_schedulers_lock:
        item = _quota_schedulers.pop(int(workspace_id), None)
    if item:
        item[0].set()
    return item


def _restore_quota_schedulers() -> int:
    """Restore every persisted quota scheduler during application startup."""
    restored = 0
    offset = 0
    page_size = 200
    while True:
        rows = db.list_workspace_masters(limit=page_size, offset=offset)
        for row in rows:
            workspace_id = int(row.get("id") or 0)
            if not workspace_id:
                continue
            settings = db.get_workspace_settings(workspace_id)
            if not settings.get("quota_enabled"):
                continue
            try:
                _, started = _start_quota_scheduler(
                    workspace_id,
                    settings,
                    source="startup",
                )
                restored += int(started)
            except Exception:
                logger.exception(
                    "启动时恢复定时额度查询任务失败 workspace_db_id=%s",
                    workspace_id,
                )
        if len(rows) < page_size:
            break
        offset += page_size
    logger.info("启动时恢复定时额度查询任务完成 restored=%s", restored)
    return restored


def _refresh_workspace_seat_info(workspace_id: int, retries: int = 3, delay_seconds: int = 5) -> dict | None:
    last_error: Exception | None = None
    for attempt in range(max(1, int(retries))):
        try:
            return workspace_membership.sync_seat_info(workspace_id)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "席位信息刷新失败 workspace_db_id=%s attempt=%s/%s",
                workspace_id,
                attempt + 1,
                retries,
                exc_info=True,
            )
            if attempt >= retries - 1:
                break
            time.sleep(max(1, int(delay_seconds)))
    logger.error("席位信息刷新放弃 workspace_db_id=%s last_error=%s", workspace_id, last_error)
    return None


def _refresh_workspace_unknown_candidate_seats(workspace_id: int, limit: int = 200) -> dict:
    """补齐当前空间里席位信息未知的候选人。

    这里的“未知”指本地没有有效 seat_type / seat_label / member_id 记录，
    但候选人已经处于 joined 且未入箱状态。刷新后会回写到候选表，供后续
    自动补标准席位、额度查询和导出直接复用。
    """
    with _workspace_member_sync_lock:
        if workspace_id in _workspace_member_sync_running:
            raise RuntimeError("该母号的成员席位正在同步，请勿重复提交")
        _workspace_member_sync_running.add(workspace_id)

    try:
        rows = db.list_workspace_candidate_options(
            workspace_id,
            account_status="active",
            join_status="joined",
            trash_status="active",
        )

        pending: list[dict] = []
        for row in rows:
            seat = workspace_membership._canonical_candidate_seat_type(
                row.get("seat_type") or row.get("seat_label") or "",
            )
            gpt_seat = str(row.get("gpt_seat") or "").strip()
            codex_seat = str(row.get("codex_seat") or "").strip()
            member_id = str(row.get("member_id") or "").strip()
            if seat in {"default", "usage_based"} and (gpt_seat or codex_seat) and member_id:
                continue
            pending.append(row)

        emails = [
            str(row.get("email") or "").strip().lower()
            for row in pending[:max(1, int(limit))]
            if str(row.get("email") or "").strip()
        ]
        if not emails:
            return {"requested": 0, "refreshed": 0, "missing": 0, "remaining": 0}

        snapshot = workspace_membership.fetch_candidate_seats_bulk(workspace_id, emails)
        refreshed = 0
        for email in emails:
            info = snapshot.get(email) or {}
            if not info:
                continue
            db.update_workspace_candidate_seats(
                workspace_id,
                email,
                info.get("codex_seat", ""),
                info.get("gpt_seat", ""),
            )
            db.update_workspace_candidate_member(
                workspace_id,
                email,
                info.get("member_id", ""),
                info.get("raw_seat_type", ""),
            )
            if info.get("member_id"):
                refreshed += 1

        logger.info(
            "成员席位独立同步完成 workspace_db_id=%s requested=%s refreshed=%s missing=%s",
            workspace_id,
            len(emails),
            refreshed,
            len(emails) - refreshed,
        )
        return {
            "requested": len(emails),
            "refreshed": refreshed,
            "missing": len(emails) - refreshed,
            "remaining": max(0, len(pending) - refreshed),
        }
    finally:
        with _workspace_member_sync_lock:
            _workspace_member_sync_running.discard(workspace_id)


def _workspace_auto_standard_candidates(workspace_id: int, seen: set[str] | None = None) -> list[dict]:
    seen = seen or set()
    rows = db.list_workspace_candidate_options(
        workspace_id,
        account_status="active",
        join_status="joined",
        seat_type="usage_based",
        trash_status="active",
    )
    out = []
    for row in rows:
        email = str(row.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        if str(row.get("workspace_join_status") or "") != "joined":
            continue
        if str(row.get("account_status") or "") == "permanently_invalid":
            continue
        if str(row.get("trash_status") or "active") == "trashed":
            continue
        if not _is_codex_seat(row.get("seat_label") or row.get("seat_type")):
            continue
        out.append(row)
    return out


def _workspace_seat_protect_exhausted(settings: dict) -> bool:
    if not settings.get("seat_protect_enabled"):
        return False
    threshold = max(1, int(settings.get("seat_protect_threshold") or 8))
    used = max(0, int(settings.get("seat_protect_used_count") or 0))
    return used >= threshold


def _switch_candidate_to_default_and_verify(workspace_id: int, candidate: dict, settings: dict) -> dict:
    email = str(candidate.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "error": "email 不能为空"}
    row = db.get_workspace_candidate(workspace_id, email) or dict(candidate)
    if str(row.get("workspace_join_status") or "") != "joined":
        return {"ok": False, "error": "候选人尚未加入当前空间"}

    current_seat = workspace_membership.resolve_candidate_seat_type(
        workspace_id,
        email,
    )
    if current_seat == "default":
        return {"ok": True, "skipped": True, "reason": "already_target_seat"}

    fresh = workspace_membership.fetch_candidate_seats(workspace_id, [email]).get(email, {})
    if not current_seat:
        current_seat = workspace_membership._canonical_candidate_seat_type(
            fresh.get("raw_seat_type") or fresh.get("seat_type") or fresh.get("seat_label")
        )
    if current_seat and current_seat not in {"default", "usage_based"}:
        return {"ok": False, "error": f"当前席位不是可切换目标：{current_seat}"}
    member_id = str(fresh.get("member_id") or row.get("member_id") or "").strip()
    if not member_id:
        return {"ok": False, "error": "未获取到成员 member_id，请先校验候选状态"}

    reserved = False
    if settings.get("seat_protect_enabled"):
        reservation = db.reserve_workspace_seat_protect_quota(workspace_id, 1)
        if not reservation.get("allowed", False):
            return {
                "ok": False,
                "error": (
                    "席位保护已生效：本周期标准席位切换已达 "
                    f"{int(reservation.get('threshold') or settings.get('seat_protect_threshold') or 8)} 个，"
                    f"下次刷新时间 {reservation.get('refresh_time') or settings.get('seat_protect_refresh_time') or '00:00'}"
                ),
                "blocked_by_protect": True,
            }
        reserved = bool(reservation.get("enabled"))

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            workspace_membership.update_member_seat_type(workspace_id, member_id, "default")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "自动标准席位切换请求失败 workspace_db_id=%s email=%s attempt=%s/3",
                workspace_id,
                email,
                attempt + 1,
                exc_info=True,
            )
        try:
            refreshed = workspace_membership.fetch_candidate_seats(workspace_id, [email]).get(email, {})
        except Exception as exc:  # noqa: BLE001
            refreshed = {}
            last_error = exc
            logger.warning(
                "自动标准席位复查失败 workspace_db_id=%s email=%s attempt=%s/3",
                workspace_id,
                email,
                attempt + 1,
                exc_info=True,
            )
        seat = workspace_membership._canonical_candidate_seat_type(
            refreshed.get("raw_seat_type") or refreshed.get("seat_type") or refreshed.get("seat_label")
        )
        if seat == "default":
            db.update_workspace_candidate_seats(
                workspace_id,
                email,
                refreshed.get("codex_seat", ""),
                refreshed.get("gpt_seat", ""),
            )
            db.update_workspace_candidate_member(
                workspace_id,
                email,
                refreshed.get("member_id", member_id),
                "default",
            )
            return {"ok": True, "email": email, "member_id": member_id, "seat": refreshed, "reserved": reserved}
        if attempt < 2:
            time.sleep(5)

    if reserved:
        try:
            db.release_workspace_seat_protect_quota(workspace_id, 1)
        except Exception:
            logger.exception("自动标准席位保护配额回滚失败 workspace_db_id=%s email=%s", workspace_id, email)
    return {"ok": False, "email": email, "member_id": member_id, "error": f"席位切换失败: {last_error or '复查未通过'}"}


def _enqueue_workspace_credentials(workspace_id: int, emails: list[str], settings: dict) -> dict:
    emails = [str(e).strip().lower() for e in emails if str(e).strip()]
    if not emails:
        return {"ok": True, "eligible": 0, "skipped": 0, "skipped_emails": []}
    master = db.get_workspace_master(workspace_id)
    if not master or not master.get("workspace_id"):
        raise RuntimeError("母号缺少 Workspace ID")
    proxy_pool = str(settings.get("proxy_pool") or "").strip()
    if not proxy_pool:
        raise RuntimeError("全局代理池为空")
    result = login_controller_for(
        workspace_db_id=workspace_id,
        workspace_id=master["workspace_id"],
        ensure_credentials=False,
    ).start({
        "login_only": True,
        "ensure_credentials": False,
        "login_emails": emails,
        "group_name": "__all__",
        "workspace_id": master["workspace_id"],
        "workspace_db_id": workspace_id,
        "proxy_pool": proxy_pool,
        "proxy": "",
        "proxy_usage_detail": "workspace_credentials",
        "concurrency": int(settings.get("concurrency", 1) or 1),
        "otp_timeout": int(settings.get("otp_timeout", 180) or 180),
        "want_access_token": True,
        "want_session_token": True,
        "want_refresh_token": True,
        "want_password": False,
        "want_2fa": False,
        "allow_existing_login": True,
        "cool_down_seconds": int(settings.get("cool_down_seconds", 0) or 0),
        "account_retry_count": int(settings.get("account_retry_count", 1) or 1),
        "auto_export": bool(settings.get("auto_push")),
        "export_refresh_oauth": False,
        "target_count": 0,
    })
    if not result.get("ok") and "已经在跑了" not in str(result.get("error") or ""):
        raise RuntimeError(result.get("error") or "空间凭证任务启动失败")
    return result


def _auto_standard_seat_worker(workspace_id: int, stop: threading.Event):
    logger.info("自动标准席位任务启动 workspace_db_id=%s", workspace_id)
    try:
        while not stop.is_set():
            settings = _workspace_settings_snapshot(workspace_id)
            if not settings.get("auto_standard_seat_enabled"):
                logger.info("自动标准席位任务已关闭 workspace_db_id=%s", workspace_id)
                return
            if _workspace_seat_protect_exhausted(settings):
                logger.info(
                    "自动标准席位任务因席位保护跳过本轮 workspace_db_id=%s used=%s threshold=%s",
                    workspace_id,
                    int(settings.get("seat_protect_used_count") or 0),
                    int(settings.get("seat_protect_threshold") or 8),
                )
                stop.wait(5 * 60)
                continue
            seat_info = _refresh_workspace_seat_info(workspace_id, retries=3, delay_seconds=5)
            if not seat_info:
                stop.wait(5 * 60)
                continue
            try:
                db.update_workspace_seat_info(workspace_id, **seat_info)
            except Exception:
                logger.exception("自动标准席位刷新写回母号失败 workspace_db_id=%s", workspace_id)
            try:
                _refresh_workspace_unknown_candidate_seats(workspace_id)
            except Exception:
                logger.exception("自动标准席位成员快照刷新失败 workspace_db_id=%s", workspace_id)
            entitled = int(seat_info.get("seats_entitled") or 0)
            current_default = int(seat_info.get("seats_default") or 0)
            if entitled <= 0 or current_default >= entitled:
                stop.wait(5 * 60)
                continue

            switched: list[str] = []
            attempted: set[str] = set()
            while not stop.is_set():
                settings = _workspace_settings_snapshot(workspace_id)
                if _workspace_seat_protect_exhausted(settings):
                    logger.info(
                        "自动标准席位任务达到席位保护阈值，停止本轮 workspace_db_id=%s used=%s threshold=%s",
                        workspace_id,
                        int(settings.get("seat_protect_used_count") or 0),
                        int(settings.get("seat_protect_threshold") or 8),
                    )
                    break
                seat_info = _refresh_workspace_seat_info(workspace_id, retries=3, delay_seconds=5)
                if not seat_info:
                    break
                try:
                    db.update_workspace_seat_info(workspace_id, **seat_info)
                except Exception:
                    logger.exception("自动标准席位刷新写回母号失败 workspace_db_id=%s", workspace_id)
                entitled = int(seat_info.get("seats_entitled") or 0)
                current_default = int(seat_info.get("seats_default") or 0)
                deficit = max(0, entitled - current_default)
                if deficit <= 0:
                    break
                candidates = _workspace_auto_standard_candidates(workspace_id, attempted)
                if not candidates:
                    logger.info(
                        "自动标准席位任务候选不足 workspace_db_id=%s deficit=%s",
                        workspace_id,
                        deficit,
                    )
                    break
                candidate = candidates[0]
                email = str(candidate.get("email") or "").strip().lower()
                attempted.add(email)
                result = _switch_candidate_to_default_and_verify(workspace_id, candidate, settings)
                if result.get("ok") and not result.get("skipped"):
                    switched.append(email)
                    logger.info(
                        "自动标准席位切换成功 workspace_db_id=%s email=%s",
                        workspace_id,
                        email,
                    )
                elif result.get("skipped"):
                    logger.info(
                        "自动标准席位候选已是目标席位 workspace_db_id=%s email=%s",
                        workspace_id,
                        email,
                    )
                elif result.get("blocked_by_protect"):
                    logger.info(
                        "自动标准席位任务受到席位保护，停止本轮 workspace_db_id=%s email=%s",
                        workspace_id,
                        email,
                    )
                    break
                else:
                    logger.warning(
                        "自动标准席位切换失败 workspace_db_id=%s email=%s error=%s",
                        workspace_id,
                        email,
                        result.get("error"),
                    )
                if stop.is_set():
                    break
                stop.wait(5)

            if switched:
                try:
                    _enqueue_workspace_credentials(workspace_id, switched, _workspace_settings_snapshot(workspace_id))
                except Exception:
                    logger.exception("自动标准席位后续凭证获取失败 workspace_db_id=%s emails=%s", workspace_id, switched)
            stop.wait(5 * 60)
    finally:
        with _seat_auto_schedulers_lock:
            current = _seat_auto_schedulers.get(workspace_id)
            if current and current[0] is stop:
                _seat_auto_schedulers.pop(workspace_id, None)
        logger.info("自动标准席位任务结束 workspace_db_id=%s", workspace_id)


_trash_sweeper_stop = threading.Event()
_trash_sweeper_thread: threading.Thread | None = None


def _trash_sweeper_worker():
    while not _trash_sweeper_stop.is_set():
        try:
            quota_lease_pools: dict[int, public_relogin.ProxyLeasePool] = {}
            for row in db.list_workspace_candidate_trash_due():
                if _trash_sweeper_stop.is_set():
                    break
                workspace_id = int(row.get("workspace_master_id") or 0)
                if not workspace_id:
                    continue
                settings = _workspace_settings_snapshot(workspace_id)
                try:
                    quota_leases = quota_lease_pools.get(workspace_id)
                    if quota_leases is None:
                        try:
                            quota_leases = _candidate_quota_proxy_pool(settings.get("proxy_pool"))
                        except ValueError:
                            # 交给单条处理函数统一记录失败并把复查时间后移，避免
                            # 到期行每 30 秒被 sweeper 反复捞起。
                            _process_scheduled_trash_due(row, settings)
                            continue
                        quota_lease_pools[workspace_id] = quota_leases
                    _process_scheduled_trash_due(row, settings, quota_leases)
                except Exception:
                    logger.exception(
                        "垃圾箱到期处理失败 workspace_db_id=%s email=%s",
                        workspace_id,
                        row.get("email", ""),
                    )
        except Exception:
            logger.exception("垃圾箱调度轮询失败")
        _trash_sweeper_stop.wait(30)


@app.on_event("startup")
def _start_background_sweeper():
    global _trash_sweeper_thread
    if not (_trash_sweeper_thread and _trash_sweeper_thread.is_alive()):
        _trash_sweeper_stop.clear()
        _trash_sweeper_thread = threading.Thread(target=_trash_sweeper_worker, daemon=True, name="trash-sweeper")
        _trash_sweeper_thread.start()
    try:
        _restore_quota_schedulers()
    except Exception:
        logger.exception("启动时恢复定时额度查询任务失败")
    try:
        for row in db.list_workspace_masters(limit=200, offset=0):
            workspace_id = int(row.get("id") or 0)
            if not workspace_id:
                continue
            settings = db.get_workspace_settings(workspace_id)
            if settings.get("auto_standard_seat_enabled"):
                with _seat_auto_schedulers_lock:
                    if workspace_id in _seat_auto_schedulers:
                        continue
                    stop = threading.Event()
                    thread = threading.Thread(
                        target=_auto_standard_seat_worker,
                        args=(workspace_id, stop),
                        daemon=True,
                        name=f"seat-auto-{workspace_id}",
                    )
                    next_at = time.time() + 5 * 60
                    _seat_auto_schedulers[workspace_id] = (stop, thread, next_at)
                    thread.start()
    except Exception:
        logger.exception("启动时恢复自动标准席位任务失败")


@app.on_event("shutdown")
def _stop_background_sweeper():
    _trash_sweeper_stop.set()
    with _quota_schedulers_lock:
        quota_items = list(_quota_schedulers.values())
        _quota_schedulers.clear()
    for item in quota_items:
        item[0].set()


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    group_name: str = Field("", description="邮箱分组；空=未分组，__all__=全部")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    want_password: bool = True  # 通用 OTP 是否强制创建可长期登录的密码
    proxy: str = ""
    otp_timeout: int = 10
    add_phone_mode: str = Field("api", description="add-phone 验证模式：api / camoufox")
    allow_existing_login: bool = True
    # 注册成功后自动绑定 TOTP 2FA。前端两个页面都**默认开**（主人要求每个号都绑）。
    # 这里的 default 保持 False —— 它只在「调用方没传这个字段」时生效，是给旧前端
    # 缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不替调用方做一个不可逆的决定。
    # 真实默认值由前端 form store 的 want2fa / autoWant2fa 决定。
    want_2fa: bool = False


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
def api_import(req: ImportReq):
    """批量导入号池。**有一行不合法就整批拒绝**，一个都不写库。

    非法时返回 422，body 里带每一行的行号和原因，前端直接展示即可：

        {"ok": false, "message": "...", "errors": [{"line": 3, "error": "..."}]}
    """
    try:
        result = db.import_accounts(req.text, kind=req.kind, group_name=req.group_name)
    except ImportValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "message": str(e), "errors": e.errors},
        )
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result, "stats": db.stats()}


@app.post("/api/registered/import_sub2api")
def api_import_sub2api(req: Sub2APIImportReq):
    try:
        payload = json.loads(req.text)
        result = db.import_sub2api_registered(payload, group_name=req.group_name)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON 解析失败: {e}")
    except ValueError as e:
        try:
            detail = json.loads(str(e))
        except Exception:
            detail = str(e)
        raise HTTPException(400, detail)
    return {"ok": True, **result}


# ──────────────────────── Team 工作空间母号 ────────────────────────


@app.post("/api/workspaces/import")
def api_import_workspace_sessions(req: WorkspaceSessionImportReq):
    try:
        result = db.import_workspace_sessions(req.text, proxy=req.proxy)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result}


@app.get("/api/workspaces")
def api_workspace_masters(limit: int = 20, offset: int = 0):
    return {
        "ok": True,
        "items": db.list_workspace_masters(limit=limit, offset=offset),
        "total": db.count_workspace_masters(),
    }


@app.post("/api/workspaces/bulk_delete")
def api_bulk_delete_workspace_masters(req: WorkspaceBulkDeleteReq):
    return {"ok": True, "deleted": db.delete_workspace_masters(req.ids)}


@app.get("/api/workspaces/{workspace_id}")
def api_workspace_master(workspace_id: int):
    row = db.get_workspace_master(workspace_id)
    if not row:
        raise HTTPException(404, "母号不存在")
    return {"ok": True, "data": row}


@app.delete("/api/workspaces/{workspace_id}")
def api_delete_workspace_master(workspace_id: int):
    if not db.delete_workspace_master(workspace_id):
        raise HTTPException(404, "母号不存在")
    return {"ok": True}


@app.post("/api/workspaces/{workspace_id}/proxy")
def api_update_workspace_proxy(workspace_id: int, req: WorkspaceProxyReq):
    try:
        ok = db.update_workspace_proxy(workspace_id, req.proxy)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "母号不存在")
    return {"ok": True}


def _workspace_candidate_index(workspace_id: int) -> dict[str, dict]:
    return {
        str(row.get("email") or "").strip().lower(): row
        for row in db.list_workspace_candidate_options(workspace_id)
    }


def _candidate_trash_enabled(workspace_id: int, settings: dict | None = None) -> bool:
    cfg = _workspace_settings_snapshot(workspace_id, settings)
    return bool(cfg.get("trash_enabled", True))


def _candidate_trash_invalid_enabled(workspace_id: int, settings: dict | None = None) -> bool:
    cfg = _workspace_settings_snapshot(workspace_id, settings)
    return bool(cfg.get("trash_invalid_enabled", True))


def _candidate_trash_delay_seconds(workspace_id: int, settings: dict | None = None) -> int:
    cfg = _workspace_settings_snapshot(workspace_id, settings)
    return int(cfg.get("trash_zero_delay_minutes", 60) or 60) * 60


def _schedule_candidate_trash(workspace_id: int, email: str, *, reason: str = "quota_zero", delay_seconds: int | None = None) -> bool:
    row = db.get_workspace_candidate(workspace_id, email)
    if not row:
        return False
    trash_status = str(row.get("trash_status") or "active")
    if trash_status == "trashed":
        return False
    if trash_status == "scheduled" and float(row.get("trash_due_at") or 0) > 0:
        # 已经排过一次入箱就不要再延后，避免每次额度刷新都把到期时间重置。
        return True
    now = time.time()
    due_at = now + max(60, int(delay_seconds or 60 * 60))
    return db.update_workspace_candidate_trash(
        workspace_id,
        email,
        status="scheduled",
        due_at=due_at,
        reason=reason,
    )


def _clear_candidate_trash_timer(workspace_id: int, email: str) -> bool:
    row = db.get_workspace_candidate(workspace_id, email)
    if not row or str(row.get("trash_status") or "active") == "active":
        return False
    return db.update_workspace_candidate_trash(workspace_id, email, status="active", due_at=0, reason="")


def _apply_candidate_trash(workspace_id: int, email: str, reason: str = "quota_zero") -> dict:
    try:
        return workspace_membership.trash_workspace_candidate(workspace_id, email, reason=reason)
    except Exception:
        logger.exception("候选人入垃圾箱失败 workspace_db_id=%s email=%s", workspace_id, email)
        raise


def _wait_and_relogin_for_candidate(workspace_id: int, email: str, settings: dict, *, auto_export: bool = False) -> bool:
    try:
        started = login_controller_for(
            workspace_db_id=workspace_id,
            workspace_id=(db.get_workspace_master(workspace_id) or {}).get("workspace_id", ""),
            ensure_credentials=False,
        ).start(_workspace_login_options(workspace_id, email, settings, auto_export=auto_export))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("候选人 401 重登录启动失败 workspace_db_id=%s email=%s", workspace_id, email)
        raise RuntimeError(f"重新登录失败: {exc}") from exc
    if not started.get("ok") and "已经在跑了" not in str(started.get("error") or ""):
        raise RuntimeError(started.get("error") or "重新登录未启动")
    timeout = int(settings.get("otp_timeout", 180) or 180) + 900
    _wait_for_login_completion(
        workspace_id,
        email,
        timeout=timeout,
        ensure_credentials=False,
    )
    account = db.get_registered(email)
    if account and account.get("account_status") == "permanently_invalid":
        if _candidate_trash_invalid_enabled(workspace_id, settings):
            try:
                workspace_membership.trash_workspace_candidates_by_email(email, reason="login_403")
            except Exception:
                logger.exception("账号失效后垃圾箱处理失败 workspace_db_id=%s email=%s", workspace_id, email)
        return False
    return True


def _process_scheduled_trash_due(
    row: dict,
    settings: dict,
    quota_leases: public_relogin.ProxyLeasePool | None = None,
) -> None:
    workspace_id = int(row.get("workspace_master_id") or 0)
    email = str(row.get("email") or "").strip().lower()
    if not workspace_id or not email:
        return
    if not _candidate_trash_enabled(workspace_id, settings):
        _clear_candidate_trash_timer(workspace_id, email)
        return
    quota_proxy = ""
    try:
        if quota_leases is None:
            quota_leases = _candidate_quota_proxy_pool(settings.get("proxy_pool"))
        quota_proxy = _lease_candidate_quota_proxy(
            quota_leases,
            workspace_id=workspace_id,
            email=email,
            detail="workspace_quota_trash_recheck",
        )
        quota = workspace_membership.fetch_candidate_quota(
            workspace_id,
            email,
            proxy=quota_proxy,
        )
    except workspace_membership.QuotaUnauthorized:
        try:
            if _wait_and_relogin_for_candidate(workspace_id, email, settings, auto_export=bool(settings.get("auto_push"))):
                retry_proxy = _lease_candidate_quota_proxy(
                    quota_leases,
                    workspace_id=workspace_id,
                    email=email,
                    detail="workspace_quota_trash_recheck",
                    exclude_proxy=quota_proxy,
                )
                quota = workspace_membership.fetch_candidate_quota(
                    workspace_id,
                    email,
                    proxy=retry_proxy,
                )
            else:
                if not _candidate_trash_invalid_enabled(workspace_id, settings):
                    _clear_candidate_trash_timer(workspace_id, email)
                return
        except Exception:
            logger.exception("垃圾箱到期复查 401 后重试失败 workspace_db_id=%s email=%s", workspace_id, email)
            db.update_workspace_candidate_trash(
                workspace_id,
                email,
                status="scheduled",
                due_at=time.time() + 10 * 60,
                reason="quota_401_retry",
            )
            return
    except Exception:
        logger.exception("垃圾箱到期额度复查失败 workspace_db_id=%s email=%s", workspace_id, email)
        db.update_workspace_candidate_trash(
            workspace_id,
            email,
            status="scheduled",
            due_at=time.time() + 10 * 60,
            reason="quota_retry",
        )
        return
    if _is_zero_quota_payload(quota):
        _apply_candidate_trash(workspace_id, email, reason="quota_zero")
    else:
        _clear_candidate_trash_timer(workspace_id, email)


@app.post("/api/workspace-candidates/trash")
def api_trash_workspace_candidates(req: WorkspaceCandidatesReq):
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    indexed = _workspace_candidate_index(req.workspace_id)
    emails = [email.lower() for email in req.emails if email.lower() in indexed]
    if not emails:
        raise HTTPException(400, "所选账号不是当前母号空间的候选人")
    results = []
    for email in emails:
        try:
            results.append({
                "email": email,
                "ok": True,
                "result": workspace_membership.trash_workspace_candidate(
                    req.workspace_id,
                    email,
                    reason="manual_trash",
                ),
            })
        except Exception as exc:
            logger.exception("候选垃圾箱处理失败 workspace_db_id=%s email=%s", req.workspace_id, email)
            results.append({"email": email, "ok": False, "error": str(exc)})
    return {
        "ok": not any(not item.get("ok") for item in results),
        "results": results,
        "trashed": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
    }


@app.get("/api/workspace-candidates/options")
def api_workspace_candidate_options(
    workspace_id: int,
    limit: int = 100,
    offset: int = 0,
    account_status: str = "",
    join_status: str = "",
    credential_status: str = "",
    seat_type: str = "",
    trash_status: str = "",
    tag_status: str = "",
):
    limit = max(1, min(1000, int(limit or 100)))
    offset = max(0, int(offset or 0))
    items = db.list_workspace_candidate_options(
        workspace_id, limit=limit, offset=offset,
        account_status=account_status, join_status=join_status,
        credential_status=credential_status, seat_type=seat_type,
        trash_status=trash_status, tag_status=tag_status,
    )
    total = db.count_workspace_candidate_options(
        workspace_id, account_status=account_status, join_status=join_status,
        credential_status=credential_status, seat_type=seat_type,
        trash_status=trash_status, tag_status=tag_status,
    )
    return {"ok": True, "items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/workspace-candidates")
def api_workspace_candidates(workspace_id: int):
    rows = db.list_workspace_candidates(workspace_id)
    # 访问令牌只用于服务端真实操作，绝不返回浏览器。
    for row in rows:
        row.pop("access_token", None); row.pop("session_token", None); row.pop("refresh_token", None); row.pop("password", None)
    return {"ok": True, "items": rows}


def _reject_permanently_invalid(emails: list[str]) -> None:
    blocked = db.list_registered_invalid_emails(emails)
    if blocked:
        raise HTTPException(409, "账号已永久失效，不能执行该任务：" + ", ".join(sorted(blocked)))


def _reject_trashed_candidates(workspace_id: int, emails: list[str]) -> None:
    indexed = _workspace_candidate_index(workspace_id)
    blocked = [
        email.lower()
        for email in emails
        if indexed.get(email.lower(), {}).get("trash_status") == "trashed"
    ]
    if blocked:
        raise HTTPException(409, "垃圾箱中的候选人不能执行该任务：" + ", ".join(sorted(set(blocked))))


@app.post("/api/workspace-candidates/assign")
def api_assign_workspace_candidates(req: WorkspaceCandidatesReq):
    return {"ok": True, "added": db.assign_workspace_candidates(req.workspace_id, req.emails)}


@app.post("/api/workspace-candidates/remove")
def api_remove_workspace_candidates(req: WorkspaceCandidatesReq):
    return {"ok": True, "removed": db.remove_workspace_candidates(req.workspace_id, req.emails)}


@app.post("/api/workspace-candidates/tag-status")
def api_update_workspace_candidate_tag_status(req: WorkspaceCandidatesReq):
    tag_status = str(getattr(req, "tag_status", "") or "").strip().lower()
    if tag_status not in {"active", "outbound"}:
        raise HTTPException(400, "tag_status 只能是 active / outbound")
    return {"ok": True, "changed": db.update_workspace_candidate_tag_status(req.workspace_id, req.emails, tag_status)}


@app.post("/api/workspace-candidates/invite")
def api_invite_workspace_candidates(req: WorkspaceCandidatesReq):
    if not req.emails: raise HTTPException(400, "请选择候选人")
    _reject_permanently_invalid(req.emails)
    _reject_trashed_candidates(req.workspace_id, req.emails)
    assigned = {r["email"] for r in db.list_workspace_candidates(req.workspace_id)}
    if any(email.lower() not in assigned for email in req.emails):
        raise HTTPException(400, "只能邀请已划分到当前母号空间的候选人")
    if req.seat_type not in {"default", "usage_based"}:
        raise HTTPException(400, "席位类型只能是标准席位或 Usage-based")
    invite_error = ""
    try:
        result = workspace_membership.invite_candidates(req.workspace_id, req.emails, seat_type=req.seat_type)
    except Exception as e:
        invite_error = str(e)
        logger.exception("候选管理母号邀请失败 workspace_db_id=%s count=%s，将继续校验邀请状态", req.workspace_id, len(req.emails))
    if _INVITE_STATUS_RECHECK_DELAY_SECONDS > 0:
        logger.info(
            "候选管理母号邀请结束后等待复查 workspace_db_id=%s delay_seconds=%s count=%s",
            req.workspace_id,
            _INVITE_STATUS_RECHECK_DELAY_SECONDS,
            len(req.emails),
        )
        time.sleep(_INVITE_STATUS_RECHECK_DELAY_SECONDS)
    recheck_error = ""
    try:
        states = workspace_membership.check_candidate_membership(
            req.workspace_id,
            req.emails,
            prefer_invites=True,
        )
    except Exception as exc:
        recheck_error = str(exc)
        logger.exception("候选邀请后状态校验失败 workspace_db_id=%s", req.workspace_id)
        states = {email.lower(): "unknown" for email in req.emails}
    if not recheck_error:
        for email in req.emails:
            db.update_workspace_candidate_status(req.workspace_id, email, states.get(email.lower(), "not_invited"))
    final_ok = not recheck_error and not any(value == "not_invited" for value in states.values())
    return {
        "ok": final_ok,
        "result": result if not invite_error else None,
        "states": states,
        "invite_error": invite_error,
        "recheck_error": recheck_error,
    }


@app.post("/api/workspace-candidates/request-join")
def api_request_workspace_join(req: WorkspaceCandidatesReq):
    if not req.emails: raise HTTPException(400, "请选择候选人")
    _reject_permanently_invalid(req.emails)
    _reject_trashed_candidates(req.workspace_id, req.emails)
    if req.seat_type not in {"default", "usage_based"}:
        raise HTTPException(400, "席位类型只能是标准席位或 Usage-based")
    rows = db.list_workspace_candidates(req.workspace_id)
    indexed = {r["email"]: r for r in rows}; results = []
    pool_values = [line.strip() for line in req.proxy_pool.splitlines() if line.strip()]
    proxy_leases = public_relogin.ProxyLeasePool(pool_values)
    def run_one(item):
        _index, email = item
        candidate = indexed.get(email.lower())
        if not candidate:
            return {"email": email, "ok": False, "error": "尚未划分到该母号空间"}
        try:
            proxy_value = req.proxy
            if not proxy_value and pool_values:
                proxy_value, _, _ = proxy_leases.lease(
                    task_type="candidate_join",
                    task_detail="candidate_join",
                )
            workspace_membership.request_join(req.workspace_id, candidate, proxy_value, seat_type=req.seat_type)
            db.update_workspace_candidate_status(req.workspace_id, email, "join_requested")
            return {"email": email, "ok": True}
        except Exception as e:
            logger.exception("候选管理子号申请失败 workspace_db_id=%s email=%s", req.workspace_id, email)
            return {"email": email, "ok": False, "error": str(e)}
    with ThreadPoolExecutor(max_workers=max(1, min(req.concurrency, len(req.emails)))) as pool:
        futures = [pool.submit(run_one, item) for item in enumerate(req.emails)]
        results = [f.result() for f in futures]
    return {"ok": True, "results": results, "succeeded": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}


@app.post("/api/workspace-candidates/check")
def api_check_workspace_candidates(req: WorkspaceCandidatesReq):
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    _reject_permanently_invalid(req.emails)
    assigned = {r["email"] for r in db.list_workspace_candidates(req.workspace_id)}
    emails = [email.lower() for email in req.emails if email.lower() in assigned]
    if not emails:
        raise HTTPException(400, "所选账号不是当前母号空间的候选人")
    try:
        states, seats = workspace_membership.check_candidate_membership(
            req.workspace_id,
            emails,
            include_seats=True,
        )
    except Exception as e:
        logger.exception("候选状态校验失败 workspace_db_id=%s", req.workspace_id)
        status_code = 429 if getattr(e, "status_code", 0) == 429 else 400
        raise HTTPException(status_code, str(e))
    for email, status in states.items():
        db.update_workspace_candidate_status(req.workspace_id, email, status)
    for email, info in seats.items():
        db.update_workspace_candidate_seats(req.workspace_id, email, info.get("codex_seat", ""), info.get("gpt_seat", ""))
        db.update_workspace_candidate_member(req.workspace_id, email, info.get("member_id", ""), info.get("raw_seat_type", ""))
    return {"ok": True, "states": states}


@app.post("/api/workspace-candidates/invite-status")
def api_update_workspace_candidate_invite_status(req: WorkspaceCandidateInviteStatusReq):
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    normalized = str(req.join_status or "").strip()
    if normalized not in {"not_invited", "pending_invite", "joined"}:
        raise HTTPException(400, "邀请状态只能是 not_invited / pending_invite / joined")
    try:
        changed = db.update_workspace_candidate_join_statuses(req.workspace_id, req.emails, normalized)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "changed": changed, "join_status": normalized}


@app.post("/api/workspace-candidates/seat")
def api_update_candidate_seat(req: WorkspaceCandidatesReq):
    if not req.emails: raise HTTPException(400, "请选择候选人")
    if req.seat_type not in {"default", "usage_based"}: raise HTTPException(400, "席位类型无效")
    indexed = {r["email"]: r for r in db.list_workspace_candidate_options(req.workspace_id)}; results = []
    def canonical_seat(value: object) -> str:
        # 历史数据可能保存过 usage-based / usagebased，统一后再比较。
        value = str(value or "").strip().lower().replace("-", "_")
        if value in {"usage_based", "usagebased", "codex席位"}:
            return "usage_based"
        if value in {"default", "standard", "standard_seat", "gpt席位", "标准席位"}:
            return "default"
        return value
    settings = db.get_workspace_settings(req.workspace_id)
    for email in req.emails:
        key = email.lower(); row = indexed.get(key)
        reserved = False
        try:
            if not row: raise RuntimeError("候选人不属于当前空间")
            if row.get("workspace_join_status") != "joined":
                raise RuntimeError("候选人尚未加入当前空间")
            current_seat = canonical_seat(row.get("seat_label") or row.get("seat_type"))
            if req.seat_type == "default" or current_seat not in {"default", "usage_based"}:
                fresh = workspace_membership.fetch_candidate_seats(req.workspace_id, [email]).get(key, {})
                if not row.get("member_id"):
                    row["member_id"] = fresh.get("member_id", "")
                current_seat = canonical_seat(
                    fresh.get("raw_seat_type")
                    or fresh.get("seat_type")
                    or fresh.get("gpt_seat")
                    or fresh.get("codex_seat")
                )
            if current_seat == req.seat_type:
                results.append({
                    "email": key,
                    "ok": True,
                    "skipped": True,
                    "reason": "already_target_seat",
                    "seat_type": req.seat_type,
                })
                continue
            if not row.get("member_id"):
                fresh = workspace_membership.fetch_candidate_seats(req.workspace_id, [email]).get(key, {})
                row["member_id"] = fresh.get("member_id", "")
            if not row.get("member_id"): raise RuntimeError("未获取到成员 member_id，请先校验候选状态")
            if req.seat_type == "default" and current_seat == "usage_based":
                reservation = db.reserve_workspace_seat_protect_quota(req.workspace_id, 1)
                if not reservation.get("allowed", False):
                    raise RuntimeError(
                        "席位保护已生效：本周期标准席位切换已达 "
                        f"{int(reservation.get('threshold') or settings.get('seat_protect_threshold') or 8)} 个，"
                        f"下次刷新时间 {reservation.get('refresh_time') or settings.get('seat_protect_refresh_time') or '00:00'}"
                    )
                reserved = bool(reservation.get("enabled"))
            result = workspace_membership.update_member_seat_type(req.workspace_id, row["member_id"], req.seat_type)
            db.update_workspace_candidate_member(req.workspace_id, email, row["member_id"], req.seat_type)
            results.append({"email": key, "ok": True, "skipped": False, "result": result})
        except Exception as e:
            if reserved:
                try:
                    db.release_workspace_seat_protect_quota(req.workspace_id, 1)
                except Exception:
                    logger.exception("席位保护配额回滚失败 workspace_db_id=%s email=%s", req.workspace_id, key)
            results.append({"email": key, "ok": False, "error": str(e)})
    skipped = sum(1 for x in results if x.get("skipped"))
    failed = sum(1 for x in results if not x.get("ok"))
    return {
        "ok": failed == 0,
        "results": results,
        "changed": len(results) - skipped - failed,
        "skipped": skipped,
        "failed": failed,
    }

@app.post("/api/workspace-candidates/quota")
def api_workspace_candidate_quota(req: WorkspaceCandidatesReq):
    if not req.emails: raise HTTPException(400, "请选择候选人")
    _reject_trashed_candidates(req.workspace_id, req.emails)
    setting_overrides = req.model_dump()
    if not str(setting_overrides.get("proxy_pool") or "").strip():
        setting_overrides.pop("proxy_pool", None)
    settings = _workspace_settings_snapshot(req.workspace_id, setting_overrides)
    try:
        quota_leases = _candidate_quota_proxy_pool(
            req.proxy_pool or settings.get("proxy_pool"),
            preferred_proxy=req.quota_proxy,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    options = {
        r["email"] for r in db.list_workspace_candidate_options(req.workspace_id)
        if r.get("has_workspace_access_token")
        and r.get("account_status") != "permanently_invalid"
        and not _is_codex_seat(r.get("seat_label") or r.get("seat_type"))
    }
    invalid_emails = db.list_registered_invalid_emails(req.emails)
    results = {}
    logging.getLogger("workspace_membership").info("额度查询开始 workspace=%s count=%s relogin_on_401=%s auto_push=%s proxy_configured=%s", req.workspace_id, len(req.emails), req.relogin_on_401, req.auto_push, bool(req.proxy_pool.strip() or str(settings.get("proxy_pool") or "").strip()))
    trash_delay = _candidate_trash_delay_seconds(req.workspace_id, settings)
    for email in req.emails:
        key = email.lower()
        if key not in options:
            if key in invalid_emails:
                results[key] = {"ok": False, "error": "账号已永久失效"}
            continue
        quota = None
        quota_proxy = ""
        try:
            quota_proxy = _lease_candidate_quota_proxy(
                quota_leases,
                workspace_id=req.workspace_id,
                email=key,
                detail="workspace_quota_manual",
            )
            quota = workspace_membership.fetch_candidate_quota(
                req.workspace_id,
                email,
                proxy=quota_proxy,
            )
            results[key] = {"ok": True, "quota": quota}
        except Exception as e:
            results[key] = {"ok": False, "error": str(e)}
            is_401 = isinstance(e, workspace_membership.QuotaUnauthorized) or "HTTP 401" in str(e) or "401" in str(e)
            if req.relogin_on_401 and is_401:
                if not str(settings.get("proxy_pool") or "").strip():
                    results[key]["relogin_error"] = "全局代理池为空，无法重新登录"
                    continue
                try:
                    relogin_ok = _wait_and_relogin_for_candidate(
                        req.workspace_id,
                        key,
                        settings,
                        auto_export=bool(req.auto_push or settings.get("auto_push")),
                    )
                    if not relogin_ok:
                        results[key]["relogin_error"] = "账号已永久失效"
                        if _candidate_trash_invalid_enabled(req.workspace_id, settings):
                            results[key]["trashed"] = True
                        continue
                    retry_proxy = _lease_candidate_quota_proxy(
                        quota_leases,
                        workspace_id=req.workspace_id,
                        email=key,
                        detail="workspace_quota_manual",
                        exclude_proxy=quota_proxy,
                    )
                    quota = workspace_membership.fetch_candidate_quota(
                        req.workspace_id,
                        key,
                        proxy=retry_proxy,
                    )
                    results[key] = {"ok": True, "quota": quota, "relogin_started": True}
                except Exception as relogin_exc:
                    results[key]["relogin_error"] = str(relogin_exc)
                    continue
            else:
                continue
        if quota is not None and _is_zero_quota_payload(quota) and _candidate_trash_enabled(req.workspace_id, settings):
            scheduled = _schedule_candidate_trash(
                req.workspace_id,
                key,
                reason="quota_zero",
                delay_seconds=trash_delay,
            )
            if scheduled:
                results[key]["trash_scheduled"] = True
                candidate_row = db.get_workspace_candidate(req.workspace_id, key) or {}
                results[key]["trash_due_at"] = candidate_row.get("trash_due_at", 0)
        elif quota is not None:
            _clear_candidate_trash_timer(req.workspace_id, key)
    return {"ok": True, "results": results}

@app.post("/api/workspace-candidates/quota-schedule/start")
def api_start_quota_schedule(req: WorkspaceQuotaScheduleReq):
    try:
        _candidate_quota_proxy_pool(req.proxy_pool)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    settings = {**req.model_dump(), "quota_enabled": True}
    db.update_workspace_settings(req.workspace_id, settings)
    item, _ = _start_quota_scheduler(
        req.workspace_id,
        settings,
        replace=True,
        source="api",
    )
    return {"ok": True, "running": True, "interval_minutes": item[2], "relogin_on_401": item[3], "next_at": item[4]}

@app.post("/api/workspace-candidates/quota-schedule/stop")
def api_stop_quota_schedule(req: WorkspaceQuotaScheduleReq):
    db.update_workspace_settings(req.workspace_id, {"quota_enabled": False})
    _stop_quota_scheduler(req.workspace_id)
    return {"ok": True, "running": False}

@app.get("/api/workspace-candidates/quota-schedule")
def api_quota_schedule_status(workspace_id: int):
    cfg = db.get_workspace_settings(workspace_id)
    with _quota_schedulers_lock:
        item = _quota_schedulers.get(workspace_id)
        if item and not item[1].is_alive():
            _quota_schedulers.pop(workspace_id, None)
            item = None
    if not item and cfg.get("quota_enabled"):
        item, _ = _start_quota_scheduler(
            workspace_id,
            cfg,
            source="status",
        )
    return {"ok": True, "running": bool(item and item[1].is_alive()), "interval_minutes": item[2] if item else int(cfg.get("interval_minutes",30)), "relogin_on_401": item[3] if item else bool(cfg.get("relogin_on_401")), "next_at": item[4] if item else 0, "settings": cfg}


@app.post("/api/workspace-candidates/auto-standard-seat/start")
def api_start_auto_standard_seat(req: WorkspaceAutoSeatReq):
    settings = db.get_workspace_settings(req.workspace_id)
    with _seat_auto_schedulers_lock:
        old = _seat_auto_schedulers.pop(req.workspace_id, None)
    if old:
        old[0].set()
    db.update_workspace_settings(req.workspace_id, {"auto_standard_seat_enabled": True})
    stop = threading.Event()
    thread = threading.Thread(target=_auto_standard_seat_worker, args=(req.workspace_id, stop), daemon=True)
    next_at = time.time() + 5 * 60
    with _seat_auto_schedulers_lock:
        _seat_auto_schedulers[req.workspace_id] = (stop, thread, next_at)
    thread.start()
    return {"ok": True, "running": True, "settings": {**settings, "auto_standard_seat_enabled": True}, "next_at": next_at}


@app.post("/api/workspace-candidates/auto-standard-seat/stop")
def api_stop_auto_standard_seat(req: WorkspaceAutoSeatReq):
    with _seat_auto_schedulers_lock:
        item = _seat_auto_schedulers.pop(req.workspace_id, None)
    if item:
        item[0].set()
    db.update_workspace_settings(req.workspace_id, {"auto_standard_seat_enabled": False})
    return {"ok": True, "running": False}


@app.get("/api/workspace-candidates/auto-standard-seat")
def api_auto_standard_seat_status(workspace_id: int):
    cfg = db.get_workspace_settings(workspace_id)
    with _seat_auto_schedulers_lock:
        item = _seat_auto_schedulers.get(workspace_id)
        if not item and cfg.get("auto_standard_seat_enabled"):
            stop = threading.Event()
            thread = threading.Thread(target=_auto_standard_seat_worker, args=(workspace_id, stop), daemon=True)
            next_at = time.time() + 5 * 60
            item = (stop, thread, next_at)
            _seat_auto_schedulers[workspace_id] = item
            thread.start()
    return {
        "ok": True,
        "running": bool(item and item[1].is_alive()),
        "next_at": item[2] if item else 0,
        "settings": cfg,
    }


@app.get("/api/workspace-candidates/task-logs")
def api_workspace_candidate_task_logs(workspace_id: int, limit: int = 120):
    master = db.get_workspace_master(workspace_id) or {}
    external_id = str(master.get("workspace_id") or "").strip()
    limit = max(1, min(int(limit or 120), 500))
    with _TASK_LOG_LOCK:
        rows = list(_TASK_LOGS)
    out = []
    for item in reversed(rows):
        db_id = item.get("workspace_db_id")
        ext_id = item.get("workspace_external_id")
        if db_id != workspace_id and (not external_id or ext_id != external_id):
            continue
        out.append(item)
        if len(out) >= limit:
            break
    out.reverse()
    return {"ok": True, "items": out, "workspace_external_id": external_id, "count": len(out)}


@app.post("/api/workspace-candidates/settings")
def api_save_workspace_candidate_settings(req: WorkspaceQuotaScheduleReq):
    db.update_workspace_settings(req.workspace_id, req.model_dump())
    return {"ok": True}


@app.post("/api/workspace-candidates/credentials")
def api_workspace_credentials(req: WorkspaceCandidatesReq):
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    _reject_trashed_candidates(req.workspace_id, req.emails)
    master = db.get_workspace_master(req.workspace_id)
    if not master or not master.get("workspace_id"):
        raise HTTPException(400, "母号缺少 Workspace ID")
    invalid = db.list_registered_invalid_emails(req.emails)
    active_emails = [email for email in req.emails if email.lower() not in invalid]
    for email in invalid:
        db.update_workspace_candidate_status(req.workspace_id, email, "permanently_invalid")
    if not active_emails:
        return {"ok": True, "run": None, "workspace_id": master["workspace_id"], "eligible": 0, "skipped": len(invalid), "skipped_emails": sorted(invalid)}
    eligible = []
    skipped = sorted(invalid)
    candidate_rows = {
        str(row.get("email") or "").strip().lower(): row
        for row in (db.get_workspace_candidate(req.workspace_id, email) for email in active_emails)
        if row
    }
    trusted_eligible = []
    need_check = []
    for email in active_emails:
        current_status = str(
            (candidate_rows.get(email) or {}).get("workspace_join_status") or ""
        ).strip()
        if current_status in {"pending_invite", "joined"}:
            trusted_eligible.append(email)
        else:
            need_check.append(email)
    if need_check:
        try:
            states = workspace_membership.check_candidate_membership(req.workspace_id, need_check)
        except Exception as exc:
            logger.exception("空间凭证任务候选状态校验失败 workspace_db_id=%s", req.workspace_id)
            status_code = 429 if getattr(exc, "status_code", 0) == 429 else 400
            raise HTTPException(status_code, str(exc))
        for email, status in states.items():
            db.update_workspace_candidate_status(req.workspace_id, email, status)
            if status == "pending_request":
                workspace_membership.approve_candidate_request(req.workspace_id, email, req.seat_type)
                db.update_workspace_candidate_status(req.workspace_id, email, "approved")
                # 审批刚完成，尚未确认已进入空间，本轮不登录，避免拿到 Personal 凭证。
                skipped.append(email)
            elif status in {"joined", "pending_invite"}:
                # 待接受邀请的成员也可以直接进入空间登录流程；登录时上游会
                # 自动接受邀请并返回目标 Team 凭证。
                eligible.append(email)
            else:
                # not_invited / pending_request / 其他状态都不能获取空间凭证。
                skipped.append(email)
    eligible = trusted_eligible + eligible
    proxy_pool = req.proxy_pool
    if not proxy_pool:
        raise HTTPException(400, "全局代理池为空")
    if not eligible:
        return {"ok": True, "run": None, "workspace_id": master["workspace_id"], "eligible": 0, "skipped": len(skipped), "skipped_emails": skipped}
    result = login_controller_for(
        workspace_db_id=req.workspace_id,
        workspace_id=master["workspace_id"],
        ensure_credentials=False,
    ).start({
        "login_only": True, "ensure_credentials": False,
        "login_emails": eligible, "group_name": "__all__",
        "workspace_id": master["workspace_id"], "workspace_db_id": req.workspace_id, "proxy_pool": proxy_pool,
        "proxy": "", "proxy_usage_detail": "workspace_credentials",
        "concurrency": req.concurrency, "otp_timeout": req.otp_timeout,
        "want_access_token": True, "want_session_token": True, "want_refresh_token": True,
        "want_password": False, "want_2fa": False, "allow_existing_login": True,
        "cool_down_seconds": req.cool_down_seconds, "account_retry_count": req.account_retry_count, "auto_export": req.auto_push,
        "export_refresh_oauth": False, "target_count": 0,
    })
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "空间凭证任务启动失败"))
    return {"ok": True, "run": result, "workspace_id": master["workspace_id"], "eligible": len(eligible), "skipped": len(skipped), "skipped_emails": skipped}


@app.post("/api/workspaces/{workspace_id}/sync")
def api_sync_workspace(workspace_id: int):
    try:
        values = workspace_membership.sync_seat_info(workspace_id)
    except workspace_membership.UpstreamHttpError as exc:
        status_code = 429 if exc.status_code == 429 else 400
        raise HTTPException(status_code, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    db.update_workspace_seat_info(workspace_id, **values)
    return {"ok": True, "data": values}


@app.post("/api/workspaces/{workspace_id}/sync-members")
def api_sync_workspace_members(workspace_id: int):
    try:
        result = _refresh_workspace_unknown_candidate_seats(workspace_id)
    except workspace_membership.UpstreamHttpError as exc:
        status_code = 429 if exc.status_code == 429 else 400
        raise HTTPException(status_code, str(exc)) from exc
    except RuntimeError as exc:
        status_code = 409 if "正在同步" in str(exc) else 400
        raise HTTPException(status_code, str(exc)) from exc
    return {"ok": True, **result}


@app.get("/api/accounts")
def api_accounts(
    status: str = "", limit: int = 50, offset: int = 0, kind: str = "",
    group_name: Optional[str] = None,
):
    try:
        items = db.list_accounts(
            status=status, limit=limit, offset=offset, kind=kind, group_name=group_name,
        )
        total = db.count_accounts(status=status, kind=kind, group_name=group_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True,
        "items": items,
        "total": total,
        "by_kind": db.stats_by_kind(),
        "groups": db.list_groups(),
    }


@app.get("/api/accounts/groups")
def api_account_groups():
    return {"ok": True, "groups": db.list_groups()}


class SetGroupReq(BaseModel):
    emails: list[str] = Field(..., min_length=1)
    group_name: str = Field("", max_length=64)


@app.post("/api/accounts/set_group")
def api_set_accounts_group(req: SetGroupReq):
    try:
        updated = db.set_accounts_group(req.emails, req.group_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "updated": updated, "groups": db.list_groups()}


class GroupNameReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class RenameGroupReq(BaseModel):
    old_name: str = Field(..., min_length=1, max_length=64)
    new_name: str = Field(..., min_length=1, max_length=64)


@app.post("/api/accounts/groups")
def api_create_account_group(req: GroupNameReq):
    try:
        db.create_group(req.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "groups": db.list_groups()}


@app.post("/api/accounts/groups/rename")
def api_rename_account_group(req: RenameGroupReq):
    try:
        moved = db.rename_group(req.old_name, req.new_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "moved": moved, "groups": db.list_groups()}


@app.delete("/api/accounts/groups/{group_name}")
def api_delete_account_group(group_name: str):
    try:
        ungrouped = db.delete_group(group_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "ungrouped": ungrouped, "groups": db.list_groups()}


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


# ──────────────────────── 代理连通性测试 ────────────────────────


@app.get("/api/proxy/usage")
def api_proxy_usage():
    """返回所有真实代理池租借的持久化累计计数。"""
    return {"ok": True, "usage": proxy_usage.snapshot()}


@app.post("/api/proxy/usage/reset")
def api_reset_proxy_usage():
    """只清空租借统计，不修改浏览器中的代理池配置。"""
    return {"ok": True, "usage": proxy_usage.reset()}


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="要测试的代理列表")
    timeout: int = Field(8, description="每个代理超时秒数")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="测试目标 URL（默认返回出口 IP）")


@app.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """并发测试代理连通性。复用真实注册流程的 create_http_session（含 socks5->socks5h
    标准化、trust_env=False），保证「测试正常」== 「跑号能用」。返回 ok / 延迟 / 出口 IP。

    协议说明：不写协议的 `ip:port` 被 curl 按 HTTP 代理处理；SOCKS5 需显式写 socks5://。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from http_client import create_http_session
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"加载 http_client 失败: {e}")

    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    timeout = max(1, min(int(req.timeout or 8), 60))
    test_url = (req.test_url or "https://api.ipify.org?format=json").strip()

    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies 不能为空")

    def _test_one(proxy: str):
        t0 = _t.perf_counter()
        try:
            sess = create_http_session(proxy=proxy)
            resp = sess.get(test_url, timeout=timeout)
            latency = int((_t.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
            ip = ""
            try:
                ip = resp.json().get("ip", "")
            except Exception:
                ip = (resp.text or "").strip()[:64]
            return {"ok": True, "latency_ms": latency, "ip": ip}
        except Exception as e:  # noqa: BLE001
            latency = int((_t.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency, "error": str(e)[:140]}

    results = {}
    with ThreadPoolExecutor(max_workers=min(20, len(proxies))) as ex:
        for proxy, res in zip(proxies, ex.map(_test_one, proxies)):
            results[proxy] = res
    return {"ok": True, "results": results}


@app.post("/api/register")
def api_register(req: RegisterReq):
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 要不要 claim 号池，由 provider 自己声明的 pooled 决定 ——
    # 原来写死 `mail_source == "cf_temp"`，加一种非池化邮箱就得改这里。
    if not provider_cls.pooled:
        # 非池化：地址由 provider 现造，用占位 account 走完后面的流程
        import time as _t
        account = {
            "email": f"{mail_source}_placeholder_{int(_t.time())}@placeholder.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "relay_url": "",
            "kind": mail_source,
        }
    elif req.email:
        account = db.claim_account(req.email)
        if not account:
            raise HTTPException(400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)")
        if (account.get("kind") or "outlook") != mail_source:
            # 号池里混放多种邮箱，点名的号必须和当前来源一致，
            # 否则会拿 Outlook 的凭证去初始化 Gmail provider
            db.release_unused(account["email"])
            raise HTTPException(
                400,
                f"{req.email} 是 {account.get('kind')} 的号，"
                f"当前邮箱来源是 {mail_source}，请先切换来源",
            )
    else:
        try:
            account = db.claim_next(kind=mail_source, group_name=req.group_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not account:
            group_label = "全部分组" if req.group_name == "__all__" else (req.group_name or "未分组")
            raise HTTPException(
                400,
                f"{group_label}没有 available 的 {provider_cls.display_name} 账号；请先批量导入",
            )

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "want_password": req.want_password,
        "proxy": req.proxy,
        "otp_timeout": int(req.otp_timeout),
        "add_phone_mode": req.add_phone_mode,
        "allow_existing_login": req.allow_existing_login,
        "want_2fa": req.want_2fa,
    }
    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """SSE 实时推送日志 + 事件。"""
    q = registrar.get_run_queue(run_id)
    if q is None:
        raise HTTPException(404, "run_id not found or finished")

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 从队列取消息（用 run_in_executor 避免阻塞 event loop）
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is None:
                    # sentinel: 任务结束
                    yield "event: end\ndata: {}\n\n"
                    break
                if msg.startswith("__EVENT__:"):
                    yield f"event: status\ndata: {msg[len('__EVENT__:'):]}\n\n"
                else:
                    yield f"event: log\ndata: {json.dumps({'line': msg}, ensure_ascii=False)}\n\n"
        finally:
            registrar.remove_run_queue(run_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 避免 nginx 缓冲
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=60)
    except Exception:
        return ""  # 心跳：返空串让 SSE 检查 disconnect


@app.get("/api/runs")
def api_runs(limit: int = 50):
    return {"ok": True, "items": db.list_runs(limit=limit)}


@app.get("/api/registered")
def api_registered(
    limit: int = 20, offset: int = 0, filter: str = "all",
    group_name: Optional[str] = None,
):
    try:
        items = db.list_registered(
            limit=limit, offset=offset, filter_rt=filter, group_name=group_name,
        )
        total = db.count_registered(filter_rt=filter, group_name=group_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "items": items, "total": total, "groups": db.list_groups()}


@app.get("/api/registered/{email}")
def api_registered_one(email: str):
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, "not found")
    return {"ok": True, "data": row}


@app.delete("/api/registered/{email}")
def api_delete_registered(email: str):
    ok = db.delete_registered(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    if req.all:
        n = db.delete_all_registered()
        return {"ok": True, "deleted": n, "by": "all"}
    if req.emails:
        n = db.delete_registered_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "需要 emails 或 all=true")


# ──────────────────────── 批量导出（文本） ────────────────────────
# ⚠️ 路由顺序：
#   - formats 是 4 段路径，不会被 3 段的 GET /api/registered/{email} 吃掉；
#   - export 是 POST，而 {email} 那两条是 GET / DELETE，也不冲突。
# 要加新格式只改 webui/export_formats.py，这里和前端都不用动。


@app.get("/api/registered/export/formats")
def api_export_formats():
    """导出格式清单，前端下拉菜单据此渲染。"""
    return {"ok": True, "formats": export_formats.list_formats()}


class ExportRegisteredReq(BaseModel):
    format: str = Field(..., description="格式 id，见 GET /api/registered/export/formats")
    emails: Optional[list[str]] = Field(None, description="要导出的 email 列表")
    all: bool = Field(False, description="true = 导出全部（跨页），忽略 emails")
    workspace_id: Optional[int] = Field(None, description="按指定 Team 空间导出其独立凭证")


@app.post("/api/registered/export")
def api_export_registered(req: ExportRegisteredReq):
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")

    if req.workspace_id:
        if not req.emails:
            raise HTTPException(400, "Team 空间导出需要 emails")
        rows = db.list_workspace_credentials_by_emails(req.workspace_id, req.emails)
    elif req.all:
        rows = db.list_registered_full(limit=100000)
    elif req.emails:
        rows = db.list_registered_by_emails(req.emails)
    else:
        raise HTTPException(400, "需要 emails 或 all=true")

    # 不跳行：勾了几个号就几行 / 几个文件，字段为空也照样出。
    # 手动导出**不做 refresh_token 刷新、不因为缺 rt 拦截**，这是和自动推送的区别。
    base = {
        "ok": True,
        "count": len(rows),
        "filename": fmt.filename_for(rows) if fmt.filename_for else fmt.filename,
        "label": fmt.label,
        "mode": fmt.mode,
        "mime": fmt.mime_for(rows) if fmt.mime_for else fmt.mime,
        # 这一批导出的 email 原样带回去 —— 前端「下载并删除」照着它删，删得准。
        # ⚠️ 必须由后端给：`all=true` 时前端手里只有当前页那 20 行，
        #    自己凑列表会漏删；而用 all/status 那种"全清"接口去删号池，
        #    会把**还没跑过的号**一起清掉。所以这里回传精确列表。
        "emails": [(r.get("email") or "") for r in rows],
    }

    if fmt.mode == "download":
        # 二进制（zip / json 文件）走 base64，前端解出来直接存盘，不弹预览
        blob = export_formats.render_bytes(rows, fmt)
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob)}

    return {**base, "text": export_formats.render_text(rows, fmt)}


# ──────────────────────── 邮箱来源配置 ────────────────────────


@app.get("/api/mail/providers")
def api_mail_providers(pooled_only: bool = False):
    """列出所有已注册的邮箱 provider 及其能力 / 配置项声明。

    前端据此渲染「邮箱来源」单选和对应的动态表单 ——
    以后加邮箱，前端一行都不用改。

        pooled_only=true  只返回能导入号池的（导入页用）
    """
    return {
        "ok": True,
        "providers": list_pooled_providers() if pooled_only else list_providers(),
        "current": db.get_setting("mail_source", "outlook"),
    }


@app.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    """字段不再写死。

    mail_source 之外的配置项由各 provider 的 config_fields 声明，
    前端原样回传，db.save_mail_config 按声明逐项存 ——
    加 provider 时这个模型不用动。
    """

    model_config = {"extra": "allow"}

    mail_source: Optional[str] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    try:
        db.save_mail_config(req.model_dump(exclude_none=True))
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail():
    """测试当前邮箱来源的连通性，具体怎么测由 provider 的 self_test() 决定。

    原来这里写死了 CF 的 api_url/domain/token 三个字段，
    换成让 provider 自检 —— 加邮箱不用回来改这个路由。
    """
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 池化 provider 的连通性绑定在具体某个号上，没号可测 ——
    # 它的"测试"就是导入时的格式校验 + 跑一次注册。
    if provider_cls.pooled:
        raise HTTPException(
            400,
            f"{provider_cls.display_name} 是号池类型，不需要单独测试；"
            f"导入时会校验格式",
        )

    try:
        provider = create_mail_provider(mail_source, db.get_mail_settings())
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"构造 {provider_cls.display_name} 失败: {e}")

    try:
        result = provider.self_test()
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")
    if not result.get("ok"):
        raise HTTPException(500, result.get("message") or "连接失败")
    return {"ok": True, "message": result.get("message", "连接成功")}


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms
    sms_api_key: Optional[str] = None              # 传 '***' 表示不修改
    sms_country: Optional[str] = None              # ID 或国家代码（'52' / 'th'）
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_fixed_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # 逗号分隔的 ID 列表，自动选号时只从这里挑
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None   # 空 = 用 provider 默认；>0 = 自定义
    sms_per_phone_timeout: Optional[str] = None    # 单号等待秒数（默认 80）


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    db.save_sms_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """测试 SMS provider 连通性：查询余额。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        balance = provider.get_balance()
        return {
            "ok": True,
            "provider": cfg["sms_provider"],
            "balance": balance,
            "message": f"连接成功，余额: {balance}",
        }
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询当前接码平台的国家排名（价格 + 库存）。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider, OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
        return {"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """返回当前平台实际有库存的国家（动态查询）；查询失败则 fallback 到静态字典。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES, create_sms_provider

    cfg = db.get_sms_internal_config()
    if provider:
        cfg["sms_provider"] = provider

    # 尝试从平台 API 动态获取有库存的国家
    if cfg.get("sms_api_key"):
        try:
            p = create_sms_provider(cfg["sms_provider"], cfg)
            rows = p.get_top_countries(service=cfg.get("sms_service") or "dr")
            countries = []
            for r in rows:
                cid = str(r.get("country") or "")
                countries.append({
                    "id": cid,
                    "name_cn": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
                    "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                    "price": r.get("price"),
                    "count": r.get("count"),
                })
            if countries:
                return {"ok": True, "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "live"}
        except Exception:
            pass

    # fallback: 静态字典
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {"id": cid, "name_cn": name, "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES}
        for cid, name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


# ──────────────────────── 自动导出 (CPA / SUB2API) ────────────────────────


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # 传 '***' 表示不修改
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # '***' 不修改
    sub2api_group_ids: Optional[str] = None  # 逗号分隔，例 "2" 或 "1,2,3"
    sub2api_timeout: Optional[str] = None


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from . import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        raise HTTPException(400, f"未知 target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"测试失败: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="选择导出目标：cpa / sub2api")


class BulkCpaPushReq(BaseModel):
    emails: list[str] = Field(..., min_length=1, description="选中的已注册账号邮箱")
    proxy: str = Field("", description="刷新 OAuth token 时使用的代理")
    workspace_id: Optional[int] = Field(None, description="按指定 Team 空间推送独立凭证")


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # 手动触发：强制启用
        try:
            out["cpa"] = exporter.export_to_cpa(cred, cpa_cfg)
        except Exception as e:
            out["cpa"] = {"ok": False, "error": str(e)}
    if "sub2api" in targets:
        sub2api_cfg = dict(cfg["sub2api"])
        sub2api_cfg["enabled"] = True
        try:
            out["sub2api"] = exporter.export_to_sub2api(cred, sub2api_cfg)
        except Exception as e:
            out["sub2api"] = {"ok": False, "error": str(e)}

    return {"ok": True, **out}


@app.post("/api/registered/push_cpa")
def api_push_registered_to_cpa(req: BulkCpaPushReq):
    """将选中的注册结果逐个转换为 CPA token JSON 并上传到已配置 CPA。"""
    from . import exporter

    emails = []
    seen = set()
    for email in req.emails:
        normalized = str(email or "").strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            emails.append(normalized)
    if not emails:
        raise HTTPException(400, "没有有效的账号邮箱")

    cfg = db.get_export_internal_config()["cpa"]
    if not cfg.get("cpa_url") or not cfg.get("cpa_mgmt_key"):
        raise HTTPException(400, "请先在“自动导出”中配置 CPA URL 和管理密钥")

    rows = (db.list_workspace_credentials_by_emails(req.workspace_id, emails)
            if req.workspace_id else db.list_registered_by_emails(emails))
    row_map = {str(row.get("email") or "").lower(): row for row in rows}
    results = []
    found_rows = []
    for email in emails:
        cred = row_map.get(email)
        if not cred:
            results.append({"email": email, "ok": False, "error": "未找到注册结果"})
            continue
        found_rows.append(cred)
    refresh_cb = (lambda cred: db.save_workspace_credential(req.workspace_id, cred)) if req.workspace_id else (lambda cred: db.update_registered_oauth_tokens(
            cred.get("email", ""), access_token=cred.get("access_token", ""), refresh_token=cred.get("refresh_token", ""), id_token=cred.get("id_token", "")))
    results.extend(exporter.push_many_to_cpa(
        found_rows,
        cfg,
        on_tokens_refreshed=refresh_cb,
        proxy=req.proxy,
    ))

    succeeded = sum(1 for item in results if item.get("ok"))
    return {
        "ok": succeeded == len(results),
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    }


class UpdateCredReq(BaseModel):
    email: str = Field(..., description="要修改的已注册账号邮箱")
    # None = 该字段不动；空串 = 主动清空。前端不填的字段就别传。
    password: Optional[str] = Field(None, description="新密码，None=不修改")
    totp_secret: Optional[str] = Field(None, description="新 TOTP secret，None=不修改")


@app.post("/api/registered/update_credentials")
def api_update_credentials(req: UpdateCredReq):
    """手动修正已注册账号的密码 / TOTP secret。

    ⚠️ 只改本地库，不会同步到 OpenAI。用途是把外部已知凭证补进来或修正记录。

    改完的值会被登录流程直接用上（registrar 的 account_callback 走
    db.get_registered，不区分数据来源），所以 totp_secret 必须过 base32
    校验 —— 脏值存进去要等真登录时才炸，那时根本看不出是手填填错的。
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    if req.password is None and req.totp_secret is None:
        raise HTTPException(400, "没有要修改的字段")
    try:
        ok = db.update_registered_manual(
            email, password=req.password, totp_secret=req.totp_secret
        )
    except ValueError as e:
        # 校验失败：把具体原因带给前端，别让用户猜哪里填错了
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"未找到已注册账号: {email}")

    changed = [n for n, v in (("密码", req.password), ("TOTP secret", req.totp_secret))
               if v is not None]
    logger.info(f"[registered] 手动修改凭证 email={email} 字段={'+'.join(changed)}")
    return {"ok": True, "email": email, "changed": changed}


# ──────────────────────── Plus 试用检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="查询代理，留空直连")


# 封号在 401/403 响应体里的措辞。OpenAI 不止一种写法，全部小写后子串匹配。
# 新措辞加在这里即可；日志会打出未匹配的 401/403 原文方便补充。
_DEACTIVATED_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "has been deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",          # violating / violation of our policies
    "potential abuse",
    "terminated",
)


def _body_text(resp) -> str:
    """安全取响应体文本，任何异常都不许打断检测循环。"""
    try:
        return (resp.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _looks_deactivated(body: str) -> bool:
    return any(m in body.lower() for m in _DEACTIVATED_MARKERS)


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """用 access_token 查询账号的 Plus 试用状态。"""
    from http_client import create_http_session

    log = logging.getLogger("webui")
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 走和注册流程同一个 create_http_session，不再自己拼 proxies dict。
    # 它负责两件这里以前漏掉的事：
    #   1) socks5:// -> socks5h://，DNS 交给代理端解析。用本地 DNS 打
    #      chatgpt.com 经常握手失败，这是「填了 SOCKS5 就检测不出来」的真正原因。
    #   2) trust_env=False + 显式空代理，代理留空时是真直连，
    #      不会被系统 HTTP_PROXY/HTTPS_PROXY 悄悄接管。
    proxy = req.proxy.strip()
    try:
        sess = create_http_session(proxy=proxy or None, impersonate="chrome110")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"创建 HTTP 会话失败: {e}")

    note = ""

    def _check(access_token: str, account_id: str = "", device_id: str = ""):
        """打一次检测请求。

        ⚠️ 这里**不再自动降级直连**。原来的行为是：代理第一次报错就永久切直连，
        后面所有号都用主人的真实 IP 去打 chatgpt.com 的账号接口，而提示只是
        结果末尾一句小字。2026-08-10 实测踩到：主人改了代理池密码，这页却还在
        用 localStorage 里的旧代理 → curl:(97) 鉴权被拒 → 静默直连。
        检测失败重试一次就好，不值得拿真实 IP 换。

        请求头按 chatgpt.com 前端真实发的补齐（Origin/Referer/ChatGPT-Account-ID/
        OAI-Device-Id）。以前只发 Authorization，缺 Origin/Referer 属于典型的
        非浏览器特征，容易被风控挑出来；account_id 从 access_token 的 JWT 里解，
        不额外请求。
        """
        nonlocal note
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": ua,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        if device_id:
            headers["OAI-Device-Id"] = device_id
        try:
            return sess.get(url, headers=headers, timeout=15)
        except Exception as e:  # noqa: BLE001
            if proxy and not note:
                # 把 curl 的错误码带出来：(97)=SOCKS5 鉴权被拒，(7)=连不上，
                # 笼统一句「代理连不通」会让人以为是网络抖动，其实是密码/配额问题。
                msg = str(e)
                if "(97)" in msg or "rejected by the SOCKS5" in msg:
                    note = "代理认证被拒（SOCKS5 (97)）—— 检查代理账号密码/配额是否已变更"
                elif "(7)" in msg:
                    note = "代理连不上（curl (7)）—— 检查代理地址端口是否可达"
                else:
                    note = f"代理请求失败（{type(e).__name__}）—— 已保持代理，未改直连"
                log.warning(f"[check_plus] {note}: {msg[:140]}")
            raise

    results = {}
    for email in req.emails:
        cred = db.get_registered(email)
        if not cred:
            results[email] = {"status": "not_found", "label": "未找到"}
            continue
        at = (cred.get("access_token") or "").strip()
        if not at:
            results[email] = {"status": "no_at", "label": "无AT"}
            continue
        # account_id 直接从 AT 的 JWT payload 解（实测 12/12 都带），不发额外请求。
        auth_claims = _get_auth(_decode_jwt_payload(at))
        account_id = str(
            auth_claims.get("chatgpt_account_id") or auth_claims.get("account_id") or ""
        ).strip()
        # device_id 库里普遍是空的（注册时没落盘），按邮箱派生一个稳定 UUID：
        # 同一个号每次检测都是同一个 device，比每次随机更像正常客户端。
        device_id = (cred.get("device_id") or "").strip() or str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"dango-check-plus:{email}")
        )
        try:
            resp = _check(at, account_id, device_id)
        except Exception as e:  # noqa: BLE001
            results[email] = {"status": "error", "label": "网络失败"}
            log.warning(f"[check_plus] {email} 请求失败: {str(e)[:140]}")
            continue
        if resp.status_code in (401, 403):
            # 401/403 的**响应体必须看**。以前这里只看状态码就贴「凭证失效」，
            # 结果是封号号 100% 显示成凭证失效：账号被封时 access_token 会被一起
            # 吊销 → 请求在这里就 401 了 → 永远走不到下面 200 分支的 is_deactivated
            # 判据。2026-08-10 实测某个被封号：JWT exp 还有 239 小时、
            # 13:53 检测还是 plus_eligible，之后被封 → 同一个 token 直接 401。
            #
            # 未过期却失效 = 被吊销，而 OpenAI 会在响应体里写明原因，
            # 所以按响应体内容区分「封号」和「单纯的凭证过期/轮换」。
            body = _body_text(resp)
            if _looks_deactivated(body):
                results[email] = {"status": "banned", "label": "封号"}
                log.info(f"[check_plus] {email} 判定封号 (HTTP {resp.status_code}): {body[:200]}")
                continue
            if resp.status_code == 401:
                results[email] = {"status": "token_invalid", "label": "凭证失效"}
                # 日志留原文：万一是没覆盖到的封号措辞，主人看一眼就能告诉我补进去。
                log.info(f"[check_plus] {email} 401 响应体: {body[:200]}")
                continue
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            log.info(f"[check_plus] {email} 403 响应体: {body[:200]}")
            continue
        if resp.status_code != 200:
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            results[email] = {"status": "error", "label": "响应非 JSON"}
            continue
        accts = data.get("accounts", {})
        if not accts:
            results[email] = {"status": "error", "label": "无账户数据"}
            continue
        info = next(iter(accts.values()))
        acct = info.get("account", {})
        ent = info.get("entitlement", {})
        promo = info.get("eligible_promo_campaigns", {})
        if acct.get("is_deactivated", False):
            results[email] = {"status": "banned", "label": "封号"}
            continue
        plan = acct.get("plan_type", "free")
        has_sub = ent.get("has_active_subscription", False)
        has_plus_promo = "plus" in promo and promo["plus"].get("id") == "plus-1-month-free"
        if plan == "plus" or has_sub:
            results[email] = {"status": "plus_active", "label": "Plus生效中"}
        elif has_plus_promo:
            results[email] = {"status": "plus_eligible", "label": "可领Plus试用"}
        else:
            results[email] = {"status": "free", "label": "Free"}

    try:
        sess.close()
    except Exception:  # noqa: BLE001
        pass

    checked_at = time.time()
    for email, info in results.items():
        # not_found / no_at / error 不写库：它们不是「检测结论」而是**没检测成**
        # （号不在库里、没凭证、代理挂了），写进去号就从 unchecked 过滤器里消失，
        # 看着像已经检测过。修好后重点一次即可。
        #
        # token_invalid **要写**（2026-08-10 改）。原先不写的理由是「凭证问题不是
        # 账号问题，换新凭证后该重查」，但实测下来：AT 没过期却 401 = 被吊销，
        # 大概率就是封号（2026-08-10 实测那个号即是）。不写库的实际后果是这号
        # 一直挂着上次的 plus_eligible，列表上显示「可领Plus试用」——比标成凭证
        # 失效误导得多。写库后 unchecked 过滤器会跳过它，正是想要的：它已经有结论了。
        if info["status"] not in ("not_found", "no_at", "error"):
            db.update_plus_check(email, {**info, "checked_at": checked_at})

    return {"ok": True, "results": results, "note": note}


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """跟 RegisterReq 复用同样的字段，auto-loop 内部传给每个 run。"""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    want_password: bool = True  # 通用 OTP 是否强制创建密码
    login_only: bool = False    # 仅投送当前分组已有注册结果，刷新登录凭证
    ensure_credentials: bool = True  # 仅登录时补齐缺失的密码 / TOTP secret
    login_no_rt_only: bool = False  # 仅对无 RT 的注册结果执行
    login_emails: Optional[list[str]] = None  # 仅登录时限定指定账号（注册结果页重登录）
    workspace_id: str = ""  # 空间凭证获取时强制选择目标 Workspace
    group_name: str = Field("", description="邮箱分组；空=未分组，__all__=全部")
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    proxy_pool: str = ""         # 多代理池（每行一个）；优先于 proxy
    concurrency: int = 1         # 并发 worker 数（1-20）
    otp_timeout: int = 10
    add_phone_mode: str = Field("api", description="add-phone 验证模式：api / camoufox")
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # 每个 worker 跑完后冷却（防风控）
    target_count: int = 0        # 目标成功数（0=不限量，达标自动停止）
    account_retry_count: int = Field(1, ge=0, le=10, description="每个账号失败后的额外重试次数")
    auto_export: bool = True  # 本次任务完成后是否自动推送到已启用的面板
    export_refresh_oauth: bool = False  # 推送前是否用 RT 刷新 Codex token
    # 批量页已放开关且**默认开**（主人要求每个号都绑）。
    # 这里的 default 仍保持 False —— 它只在「前端没传这个字段」时生效，
    # 是给旧前端缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不要
    # 替调用方做一个不可逆的决定。真实默认值由 AutoLoop.vue 的 autoWant2fa 决定。
    want_2fa: bool = False


def _controller_for_options(options: dict):
    """按任务语义选择控制器：login_only 都属于同一种登录任务。"""
    if not bool((options or {}).get("login_only")):
        return AUTO_LOOP
    return login_controller_for(
        workspace_db_id=(options or {}).get("workspace_db_id"),
        workspace_id=(options or {}).get("workspace_id", ""),
        ensure_credentials=bool((options or {}).get("ensure_credentials", True)),
        login_no_rt_only=bool((options or {}).get("login_no_rt_only")),
    )


def _active_auto_controller():
    """兼容旧版暂停/停止接口，优先控制当前正在运行的登录任务。"""
    for controller in all_login_controllers():
        if controller.status().get("state") in ("running", "paused"):
            return controller
    return AUTO_LOOP


@app.post("/api/auto/start")
def api_auto_start(req: AutoLoopStartReq):
    res = _controller_for_options(req.model_dump()).start(req.model_dump())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "启动失败"))
    return res


@app.post("/api/auto/pause")
def api_auto_pause():
    res = _active_auto_controller().pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "暂停失败"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume():
    res = _active_auto_controller().resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "恢复失败"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop():
    res = _active_auto_controller().stop()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "停止失败"))
    return res


@app.get("/api/auto/status")
def api_auto_status():
    active = _active_auto_controller()
    return {
        "ok": True,
        **active.status(),
        "register_status": AUTO_LOOP.status(),
        "login_status": LOGIN_CONTROLLER.status(),
    }


@app.get("/api/auto/stream")
async def api_auto_stream(request: Request):
    """SSE 推送 auto-loop 状态变化 + run_started / run_finished 事件。"""
    # 注册和登录是两个独立控制器；合并事件流后，原有前端无需区分
    # 任务来源，也能看到手动登录、空间凭证获取和注册任务的进度。
    subscriptions = [(AUTO_LOOP, AUTO_LOOP.subscribe())]
    known_controllers = {id(AUTO_LOOP)}

    async def gen():
        idle_since = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 空间首次执行时才会创建对应的登录控制器，动态补订阅。
                for controller in all_login_controllers():
                    if id(controller) not in known_controllers:
                        subscriptions.append((controller, controller.subscribe()))
                        known_controllers.add(id(controller))
                delivered = False
                for _, q in list(subscriptions):
                    try:
                        msg = q.get_nowait()
                    except queue.Empty:
                        continue
                    delivered = True
                    if msg is None:
                        continue
                    kind = msg.get("kind", "state")
                    data = msg.get("data", {})
                    yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if delivered:
                    idle_since = time.monotonic()
                    continue
                if time.monotonic() - idle_since >= 30:
                    yield ": heartbeat\n\n"
                    idle_since = time.monotonic()
                await asyncio.sleep(0.2)
        finally:
            for controller, q in subscriptions:
                controller.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.app:app", host="127.0.0.1", port=8765, reload=False)
