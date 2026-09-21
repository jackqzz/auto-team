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
import random
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
    all_task_controllers,
    login_controller_for,
    stop_login_controllers_for_workspace,
    task_controller_for,
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

app = FastAPI(title="team-auto", docs_url=None, redoc_url=None)

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
    # /api/redeem 只能精确匹配：/api/redeem-codes 是管理端接口，不能被公开放行。
    return (
        path.startswith("/api/auth")
        or path.startswith("/api/public-relogin")
        or path == "/api/redeem"
    )


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
    relay_suffix: str = Field(
        "",
        description="追加到每行 OTP 中转链接尾部的自定义串，如 ?json=1；仅对有中转链接的行生效",
        max_length=256,
    )


class Sub2APIImportReq(BaseModel):
    text: str = Field(..., description="Sub2API JSON 文件内容")
    group_name: str = Field("", description="导入账号分组")


class Import2FAReq(BaseModel):
    text: str = Field(..., description="每行一个：邮箱----密码----2FA（2FA 可选）")
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
    tags: list[str] = Field(default_factory=list)
    tag_mode: str = "add"
    auto_push: bool = False
    relogin_on_401: bool = False
    concurrency: int = Field(1, ge=1, le=20)
    otp_timeout: int = Field(180, ge=10, le=600)
    account_retry_count: int = Field(1, ge=1, le=5)
    cool_down_seconds: int = Field(0, ge=0, le=3600)
    trash_enabled: bool = True
    trash_invalid_enabled: bool = True
    trash_zero_delay_minutes: int = Field(60, ge=1, le=1440)
    trash_zero_quota_window: str = Field("any", description="额度耗尽判定窗口：any / five_hour / weekly")
    trash_gap_seconds: int = Field(30, ge=0, le=600, description="连续入箱之间的等待秒数（串行限速）")


class WorkspaceExportOutboundReq(BaseModel):
    """将当前 Workspace 的候选人加密导出并标记为已出库。"""
    workspace_id: int
    emails: list[str] = Field(default_factory=list)
    proxy_pool: str = Field("", description="刷新 Sub2 OAuth token 使用的代理池")
    refresh_oauth: Optional[bool] = Field(
        None,
        description="是否在导出前用 refresh_token 刷新；留空跟随全局设置",
    )


class WorkspaceCandidateInviteStatusReq(BaseModel):
    workspace_id: int
    emails: list[str] = Field(default_factory=list)
    join_status: str = Field(..., description="not_invited / pending_invite / joined")


class WorkspaceResetCreditReq(BaseModel):
    """额度重置券的查看与兑换。

    刻意只接受单个 email：兑换是不可逆的消耗动作（上游 2xx 即扣券），批量入口
    容易一次误烧掉一批券，所以手动通道一次只处理一个账号。
    """
    workspace_id: int
    email: str = Field(..., description="候选人邮箱")
    proxy_pool: str = Field("", description="留空时回退到空间的候选人代理池")
    credit_id: str = Field("", description="指定兑换的券 id；留空取第一张可用的")


class WorkspaceQuotaScheduleReq(BaseModel):
    workspace_id: int
    interval_minutes: int = Field(30, ge=1, le=1440)
    relogin_on_401: bool = False
    proxy_pool: str = ""
    quota_proxy_pool: str = Field("", description="候选人专属代理池；为空时回退到全局池")
    automation_paused: bool = Field(False, description="暂停本空间全部自动化任务（定时额度/席位补齐/垃圾箱回收）")
    auto_push: bool = False
    auto_push_sub2api_enabled: bool = Field(True, description="本空间自动推送是否启用 Sub2API 目标")
    auto_push_cpa_enabled: bool = Field(True, description="本空间自动推送是否启用 CPA 目标")
    auto_push_sub2api_url: str = Field("", description="空间专属 Sub2API 地址；留空跟随全局导出配置")
    auto_push_sub2api_api_key: str = Field("", description="空间专属 Sub2API API Key；留空跟随全局导出配置")
    auto_push_sub2api_group_ids: str = Field("", description="自动推送的 Sub2API 号池分组 ID（如 '2,5'）；留空跟随全局导出配置")
    auto_push_cpa_url: str = Field("", description="空间专属 CPA 地址；留空跟随全局导出配置")
    auto_push_cpa_mgmt_key: str = Field("", description="空间专属 CPA 管理密钥；留空跟随全局导出配置")
    cpa_static_proxy_enabled: bool = Field(False, description="启用后自动推送 CPA 时从静态家宽池分配凭证级代理")
    cpa_static_proxy_pool: str = Field("", description="CPA 静态家宽代理池（每行一个）；自动推送 CPA 时按租用计数最少分配并写入凭证 proxy_url")
    auto_push_skip_codex_seat: bool = Field(True, description="Codex/Usage-based 席位跳过自动推送；仅影响自动流程，手动推送不受限")
    concurrency: int = Field(1, ge=1, le=20)
    otp_timeout: int = Field(180, ge=10, le=600)
    account_retry_count: int = Field(1, ge=1, le=5)
    cool_down_seconds: int = Field(0, ge=0, le=3600)
    quota_network_retries: int = Field(2, ge=0, le=5, description="额度查询网络/5xx 失败的重试次数")
    quota_auto_reset_enabled: bool = Field(
        False,
        description="定时额度查到耗尽时自动兑换一张重置券；券不可逆，默认关闭",
    )
    trash_enabled: bool = True
    trash_invalid_enabled: bool = True
    trash_zero_delay_minutes: int = Field(60, ge=1, le=1440)
    trash_zero_quota_window: str = Field("any", description="额度耗尽判定窗口：any / five_hour / weekly")
    trash_gap_seconds: int = Field(30, ge=0, le=600, description="连续入箱之间的等待秒数（串行限速）")
    seat_protect_enabled: bool = False
    seat_protect_threshold: int = Field(8, ge=1, le=1000)
    seat_protect_refresh_time: str = Field("00:00", description="席位保护阈值刷新时间（HH:MM，CST）")
    prolite_seat_protect_enabled: bool = False
    prolite_seat_protect_threshold: int = Field(8, ge=1, le=1000)
    prolite_seat_protect_refresh_time: str = Field("00:00", description="高级席位保护阈值刷新时间（HH:MM，CST）")
    auto_standard_seat_enabled: bool = False
    auto_prolite_seat_enabled: bool = False
    auto_standard_seat_target: int = Field(0, ge=0, le=100000, description="标准席位补齐目标；0 表示已购席位上限")
    auto_prolite_seat_target: int = Field(0, ge=0, le=100000, description="高级席位补齐目标；0 表示已购席位上限")
    auto_seat_interval_minutes: int = Field(5, ge=1, le=1440, description="自动补齐席位轮询周期（分钟）")
    auto_seat_switch_gap_seconds: int = Field(30, ge=0, le=600, description="串行补齐时两次成员席位切换的间隔（秒）")
    auto_prolite_candidate_seat_type: str = Field("default", description="自动补齐高级席位的候选人席位类型")
    kick_delay_min_seconds: int = Field(2, ge=0, le=600, description="批量踢出成员的随机等待下限（秒）")
    kick_delay_max_seconds: int = Field(5, ge=0, le=600, description="批量踢出成员的随机等待上限（秒）")


class WorkspaceAutoSeatReq(BaseModel):
    workspace_id: int


class AdminLoginReq(BaseModel):
    password: str = Field(..., min_length=1)


class PublicReloginSettingsReq(BaseModel):
    public_relogin_enabled: bool = False
    proxy_pool: str = ""
    use_system_proxy_pool: bool = True
    concurrency: int = Field(3, ge=1, le=100)
    quota_queue_capacity: int = Field(512, ge=1, le=10000)
    relogin_queue_capacity: int = Field(128, ge=1, le=10000)
    retry_count: int = Field(2, ge=0, le=5)
    quota_timeout: int = Field(30, ge=5, le=120)
    login_timeout: int = Field(180, ge=30, le=900)
    rate_limit_retries: int = Field(2, ge=0, le=5, description="429 退避重试次数；0 表示不退避")
    forbidden_streak: int = Field(2, ge=1, le=10, description="连续多少次 403 才判定账号停用")
    payment_dead_accounts: int = Field(
        3, ge=1, le=50, description="同一 workspace 下多少个不同账号 402 才判定空间死亡")
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


class PublicReloginExportRefreshReq(BaseModel):
    account: dict = Field(default_factory=dict)
    access_key: str = ""
    proxy: str = ""
    proxy_pool: str = Field("", description="公开页本次导出使用的代理池（兼容旧客户端）")


_QuotaScheduler = tuple[threading.Event, threading.Thread, int, bool, float]
_quota_schedulers_lock = threading.Lock()
_quota_schedulers: dict[int, _QuotaScheduler] = {}
_seat_auto_schedulers_lock = threading.Lock()
_seat_auto_schedulers: dict[int, tuple[threading.Event, threading.Thread, float]] = {}
_prolite_auto_schedulers_lock = threading.Lock()
_prolite_auto_schedulers: dict[int, tuple[threading.Event, threading.Thread, float]] = {}
_deleted_workspace_ids: set[int] = set()
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
    """Temporarily keep the legacy parameter but ignore its value.

    The public relogin page is currently shared without an access-key gate.
    The request field and helper remain for wire compatibility with older
    clients; restoring validation later only requires changing this helper.
    """
    return {}


def _public_relogin_validate(account: dict, cfg: dict) -> tuple[dict | None, dict | None]:
    try:
        normalized = public_relogin.normalized_account(account)
    except Exception as exc:
        return None, {"ok": False, "status": "invalid", "error": f"账号格式无效: {exc}"}
    return normalized, None


def _public_relogin_dead_workspace_result(normalized: dict) -> dict:
    """已判死空间的统一返回体。

    复用 deactivated 这个既有终态：前端已经把它渲染成红标「已停用」，并且会把
    这些账号排除出后续巡检和批量重登，正好是判死想要的效果。
    """
    workspace_id = normalized.get("chatgpt_account_id", "")
    return {
        "ok": False,
        "status": "deactivated",
        "email": normalized.get("email", ""),
        "workspace_id": workspace_id,
        "error": f"workspace {workspace_id} 已判定死亡（402），已跳过；可在后台配置页手动解除",
    }


def _run_public_relogin_account(
    account: dict,
    normalized: dict,
    cfg: dict,
    proxy_leases: public_relogin.ProxyLeasePool,
    *,
    initial_exclude_proxy: str = "",
) -> dict:
    """执行单账号公开重登；巡检和手动重登共用同一套代理轮换规则。"""
    # 判死空间里的账号重登也是白费——402 是空间级的订阅/付款问题，换号换代理
    # 都救不回来。放在最前面，一次登录尝试和一次代理租借都不消耗。
    if db.is_public_workspace_dead(normalized.get("chatgpt_account_id", "")):
        result = _public_relogin_dead_workspace_result(normalized)
        result["attempt"] = 0
        return result
    account_proxy = str(account.get("proxy") or "").strip()
    if not account_proxy:
        # 已推送 CPA 的账号绑定了家宽代理，重登固定走同一出口保持 IP 粘性；
        # 语义与账号自带 proxy 一致（粘性优先，不轮换）。
        bound = db.get_cpa_proxy_lease_for_email(normalized.get("email", ""))
        if bound:
            account_proxy = bound
            proxy_usage.record_lease(bound, "login", "public_401_relogin_cpa_bound")
            logger.info(
                "公开401重登使用 CPA 绑定家宽代理 account=%s proxy=%s",
                normalized.get("email", ""), db._mask_proxy(bound),
            )
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


# 判死名单的读写只开给管理端：_is_public_api_path 只放行 /api/auth 和
# /api/public-relogin，落在 /api/settings 下就自动受管理员鉴权中间件保护，
# 公开页既看不到名单也解除不了。
@app.get("/api/settings/public-relogin/dead-workspaces")
def api_list_dead_public_workspaces():
    return {"ok": True, "workspaces": db.list_dead_public_workspaces()}


@app.delete("/api/settings/public-relogin/dead-workspaces/{workspace_id}")
def api_clear_dead_public_workspace(workspace_id: str):
    if not db.clear_dead_public_workspace(workspace_id):
        raise HTTPException(404, "该 workspace 不在判死名单里")
    return {"ok": True, "workspaces": db.list_dead_public_workspaces()}


@app.post("/api/public-relogin/check")
async def api_public_relogin_check(req: PublicReloginCheckReq):
    logger.info(
        "公开额度检查请求收到 accounts=%s proxy_pool_chars=%s auto_relogin=%s",
        len(req.accounts or []), len(str(req.proxy_pool or "")), bool(req.auto_relogin_on_401),
    )
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
    # 判死名单整批只查一次库，之后在内存里判。500 个账号跑一轮就省下 500 次
    # 查询——这正是把判死记录落后端的意义。集合在本批内还会随新判死增长，
    # 所以排在后面的同空间账号立刻受益，不用等下一轮。
    dead_workspaces = {
        str(row.get("workspace_id") or "")
        for row in db.list_dead_public_workspaces()
    }
    dead_lock = threading.Lock()

    def check_one(index: int, account: dict) -> tuple[str, dict]:
        key = _public_relogin_account_key(account, index)
        normalized, error = _public_relogin_validate(account, cfg)
        if error:
            return key, error
        workspace_id = normalized.get("chatgpt_account_id", "")
        with dead_lock:
            already_dead = bool(workspace_id) and workspace_id in dead_workspaces
        if already_dead:
            return key, _public_relogin_dead_workspace_result(normalized)
        proxy = str(account.get("proxy") or "").strip()
        if not proxy:
            # 已推送 CPA 的账号固定走绑定的家宽代理，保持出口 IP 一致。
            bound = db.get_cpa_proxy_lease_for_email(normalized.get("email", ""))
            if bound:
                proxy = bound
                proxy_usage.record_lease(bound, "quota", "public_quota_cpa_bound")
        if not proxy:
            proxy, _, _ = proxy_leases.lease(
                task_type="quota",
                task_detail="public_quota",
            )
        try:
            # 额度查询也可能遇到失效/超时代理。网络类异常换下一条代理
            # 重试，避免一个坏出口直接把账号标成错误；401/403/402 等账号或
            # 空间级响应不换代理，交给下面的状态分支处理。429 例外：限流
            # 多半按出口 IP 算，换一条出口正是正确的升级路径。
            quota = None
            last_network_error = None
            for quota_attempt in range(1, 4):
                try:
                    quota = public_relogin.fetch_quota(
                        normalized or account,
                        proxy=proxy,
                        timeout=cfg["quota_timeout"],
                        rate_retries=cfg["rate_limit_retries"],
                        forbidden_streak=cfg["forbidden_streak"],
                        dead_threshold=cfg["payment_dead_accounts"],
                    )
                    break
                except (
                    public_relogin.PublicQuotaUnauthorized,
                    public_relogin.PublicAccountDeactivated,
                    public_relogin.PublicQuotaForbidden,
                    public_relogin.PublicPaymentRequired,
                ):
                    raise
                except Exception as exc:
                    last_network_error = exc
                    account_proxy = str(account.get("proxy") or "").strip()
                    if account_proxy or quota_attempt >= 3:
                        raise
                    replacement, _, _ = proxy_leases.lease(
                        exclude_proxy=proxy,
                        task_type="quota",
                        task_detail="public_quota_retry",
                    )
                    if not replacement or replacement == proxy:
                        raise
                    logger.warning(
                        "公开额度查询切换代理 account=%s attempt=%s reason=%s",
                        normalized.get("email", ""), quota_attempt,
                        str(exc)[:180],
                    )
                    proxy = replacement
            if quota is None and last_network_error is not None:
                raise last_network_error
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
        except public_relogin.PublicWorkspaceDead as exc:
            # 必须排在 PublicAccountDeactivated 之前：它是子类，写在后面会被
            # 父类分支吃掉，判死原因和空间号就丢了。
            with dead_lock:
                dead_workspaces.add(workspace_id)
            logger.error(
                "公开额度查询判定空间死亡 account=%s workspace=%s error=%s",
                normalized.get("email", ""), workspace_id, str(exc)[:300],
            )
            return key, {"ok": False, "status": "deactivated", "email": normalized.get("email", ""), "workspace_id": workspace_id, "error": str(exc)[:500]}
        except public_relogin.PublicAccountDeactivated as exc:
            return key, {"ok": False, "status": "deactivated", "email": normalized.get("email", ""), "workspace_id": normalized.get("chatgpt_account_id", ""), "error": str(exc)}
        except (public_relogin.PublicQuotaForbidden, public_relogin.PublicPaymentRequired) as exc:
            # 缓刑：落 error 而不是 deactivated。error 在前端是非终态，下一轮
            # 巡检还会再查这个账号——这正是"缓刑"的含义。
            logger.warning(
                "公开额度查询缓刑 account=%s workspace=%s error=%s",
                normalized.get("email", ""), workspace_id, str(exc)[:300],
            )
            return key, {"ok": False, "status": "error", "email": normalized.get("email", ""), "workspace_id": workspace_id, "error": str(exc)[:500]}
        except Exception as exc:
            logger.warning(
                "公开额度查询失败 account=%s workspace=%s proxy=%s error=%s",
                normalized.get("email", ""),
                normalized.get("chatgpt_account_id", ""),
                "configured" if proxy else "direct",
                str(exc)[:300],
            )
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


@app.post("/api/public-relogin/refresh-export")
def api_public_relogin_refresh_export(req: PublicReloginExportRefreshReq):
    """将一个公开页账号刷新成 AT/RT/ID 同源的 Sub2 凭证。"""
    cfg = public_relogin.get_effective_config()
    if not cfg["enabled"]:
        raise HTTPException(403, "公开 401 重登录页面未启用")
    _validate_public_relogin_access_key(req.access_key)
    raw_account = req.account or {}
    cred = public_relogin.normalized_account(raw_account)
    access_token = str(cred.get("access_token") or "").strip()
    refresh_token = str(cred.get("refresh_token") or "").strip()
    id_token = str(cred.get("id_token") or "").strip()
    # The public page normally sends the proxy selected from its local pool.
    # Keep server-side fallbacks as well: older clients did not send ``proxy``
    # and would therefore refresh OAuth directly from the WebUI host, even
    # though a dedicated public-relogin proxy pool was configured.
    raw_credentials = raw_account.get("credentials") if isinstance(raw_account, dict) else {}
    if not isinstance(raw_credentials, dict):
        raw_credentials = {}
    refresh_proxy = str(
        req.proxy
        or raw_account.get("proxy")
        or raw_credentials.get("proxy")
        or ""
    ).strip()
    if not refresh_proxy:
        # Prefer the pool sent by the browser (it may be the local system pool
        # that is not persisted in the server config), then use the configured
        # public-relogin pool as a final fallback.
        fallback_pool = (
            _public_relogin_proxy_pool(req.proxy_pool, "")
            if bool(cfg.get("use_system_proxy_pool", True))
            else []
        )
        if not fallback_pool:
            fallback_pool = _public_relogin_effective_proxy_pool("", cfg)
        if fallback_pool:
            refresh_proxy, proxy_index, lease_count = public_relogin.ProxyLeasePool(
                fallback_pool
            ).lease(task_type="other", task_detail="public_relogin_oauth_refresh")
            logger.info(
                "公开重登 OAuth 刷新使用服务端代理池 index=%s leased_count=%s",
                proxy_index + 1,
                lease_count,
            )
    try:
        from . import exporter as _exp

        if refresh_token:
            fresh = _exp.refresh_codex_token(
                refresh_token,
                timeout=cfg["quota_timeout"],
                proxy=refresh_proxy,
                client_id=_exp.CODEX_CLIENT_ID,
            )
            access_token = str(fresh.get("access_token") or "").strip()
            refresh_token = str(fresh.get("refresh_token") or refresh_token).strip()
            id_token = str(fresh.get("id_token") or "").strip()
        _exp.validate_sub2_token_pair(access_token, id_token)
    except Exception as exc:
        raise HTTPException(502, f"Sub2 导出凭证刷新失败：{exc}") from exc
    refreshed = public_relogin.normalized_account({
        **cred,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "client_id": _decode_jwt_payload(access_token).get("client_id") or _exp.CODEX_CLIENT_ID,
    })
    refreshed["expires_at"] = int(_decode_jwt_payload(access_token).get("exp") or 0)
    return {"ok": True, "account": refreshed}


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

def _is_non_default_seat(value: object) -> bool:
    """Check if the seat is a Codex/usage-based type excluded from quota queries.

    ProLite also has a workspace credential and must remain queryable; it is
    intentionally not treated as a quota-excluded seat here.
    """
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized in {"usage_based", "usagebased", "codex", "codex席位"}


def _candidate_quota_ineligible_reason(row: dict | None, *, strict_seat: bool = True) -> str:
    if not row:
        return "候选人不属于当前空间"
    if str(row.get("account_status") or "active") == "permanently_invalid":
        return "账号已永久失效"
    # 定时额度查询及其 401 重登只面向当前仍在正常候选池中的成员。
    # scheduled 代表已经排队入箱，也不能继续参与下一轮查询。
    trash_status = str(row.get("trash_status") or "active").strip().lower()
    if trash_status != "active":
        return "候选人已在垃圾箱" if trash_status == "trashed" else "候选人已排队入箱"
    tag_status = str(row.get("tag_status") or "active").strip().lower()
    if tag_status == "outbound":
        return "候选人已出库"
    join_status = str(row.get("workspace_join_status") or "").strip().lower()
    # 真实候选列表总会带计算后的 workspace_join_status；兼容旧调用方
    # 未提供该字段时，以已有 Team 凭证视为已加入。
    if join_status and join_status != "joined":
        return "候选人尚未加入当前空间"
    if not bool(row.get("has_workspace_access_token")):
        return "未获得当前空间凭证"
    seat = str(row.get("seat_label") or row.get("seat_type") or "").strip().lower().replace("-", "_")
    if _is_non_default_seat(seat):
        return "Codex席位不参与额度查询"
    # 非 Codex 的可参与席位目前只有标准席位和 ProLite。未知席位不应
    # 因为本地缓存缺失而误进入自动任务（定时查询/自动重置），待席位
    # 同步后再参与；手动额度查询不受此限——wham/usage 并不依赖席位
    # 类型，席位未知一样可以调。
    if strict_seat and seat not in {"default", "standard", "standard_seat", "gpt席位", "标准席位", "prolite", "pro_lite", "advanced", "advanced_seat", "premium", "premium_seat", "pro", "高级", "高级席位"}:
        return "席位类型未知，暂不参与额度查询"
    return ""


def _workspace_settings_snapshot(workspace_id: int, overrides: dict | None = None) -> dict:
    cfg = dict(db.get_workspace_settings(workspace_id))
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if k != "workspace_id"})
    return cfg


def _workspace_automation_paused(settings: dict | None) -> bool:
    """空间级自动化总开关：暂停时定时额度/席位补齐/垃圾箱回收都空转。"""
    return bool((settings or {}).get("automation_paused"))


def _workspace_exists(workspace_id: int) -> bool:
    """Return whether a workspace has been marked for deletion.

    Deletion marks the id before taking the database lock, allowing in-flight
    workers to stop without another database lookup (and without mistaking a
    transient database read failure for a deleted workspace).
    """
    return int(workspace_id or 0) not in _deleted_workspace_ids


def _proxy_pool_values(value: object) -> list[str]:
    return list(dict.fromkeys(
        line.strip()
        for line in str(value or "").splitlines()
        if line.strip()
    ))


def _candidate_proxy_pool_text(settings: dict | None, req_pool: object = "") -> str:
    """候选人任务实际生效的代理池文本。

    优先级：空间专属的 ``quota_proxy_pool`` > 请求带上来的池 > 空间的
    ``proxy_pool``。前端各处仍会把全局池塞进请求，所以专属池必须排在请求
    之前，否则配了也不会生效；专属池为空时行为与改造前完全一致。
    """
    cfg = settings or {}
    dedicated = str(cfg.get("quota_proxy_pool") or "").strip()
    if dedicated:
        return dedicated
    requested = str(req_pool or "").strip()
    if requested:
        return requested
    return str(cfg.get("proxy_pool") or "").strip()


def _candidate_quota_proxy_pool(
    proxy_pool: object,
    *,
    preferred_proxy: str = "",
) -> public_relogin.ProxyLeasePool:
    values = _proxy_pool_values(proxy_pool)
    if not values:
        raise ValueError("候选人代理池为空，额度查询无法租取代理")
    preferred = str(preferred_proxy or "").strip()
    if preferred:
        if preferred not in values:
            raise ValueError("额度任务预选代理不属于当前候选人代理池")
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
    # 已推送 CPA 且绑定了家宽代理的账号固定走同一出口，与 CPA 凭证的
    # proxy_url 保持 IP 一致；仅当调用方明确要求排除（通常是该代理刚失败
    # 的重试）才回退到候选人代理池。
    bound = db.get_cpa_proxy_lease(workspace_id, email)
    if bound and bound != exclude_proxy:
        proxy_usage.record_lease(bound, "quota", detail)
        logging.getLogger("workspace_membership").info(
            "候选额度查询使用 CPA 绑定家宽代理 workspace=%s email=%s proxy=%s detail=%s",
            workspace_id,
            email,
            db._mask_proxy(bound),
            detail,
        )
        return bound
    proxy, index, leased_count = leases.lease(
        exclude_proxy,
        task_type="quota",
        task_detail=detail,
        skip_cooldown=True,
    )
    if not proxy:
        raise ValueError("候选人代理池为空，额度查询无法租取代理")
    logging.getLogger("workspace_membership").info(
        "候选额度查询领取代理 workspace=%s email=%s pool_index=%s leased_count=%s detail=%s",
        workspace_id,
        email,
        index + 1,
        leased_count,
        detail,
    )
    return proxy

TRASH_ZERO_QUOTA_WINDOWS = ("any", "five_hour", "weekly")

# 上游 wham/usage 的 limit_window_seconds：5 小时窗口 18000，周窗口 604800。
# 允许一点误差是因为上游偶尔会给出 17999/604799 之类的边界值。
_QUOTA_WINDOW_SECONDS = {"five_hour": 18000, "weekly": 604800}
_QUOTA_WINDOW_TOLERANCE = 600


def _normalize_trash_zero_quota_window(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in TRASH_ZERO_QUOTA_WINDOWS else "any"


def _candidate_trash_zero_quota_window(workspace_id: int, settings: dict | None = None) -> str:
    cfg = _workspace_settings_snapshot(workspace_id, settings)
    return _normalize_trash_zero_quota_window(cfg.get("trash_zero_quota_window"))


def _quota_window_is_exhausted(window: object) -> bool:
    if not isinstance(window, dict):
        return False
    used = window.get("used_percent")
    try:
        return used is not None and float(used) >= 100
    except (TypeError, ValueError):
        return False


def _quota_windows_by_kind(payload: dict) -> dict[str, dict]:
    """按 window_seconds 把额度窗口归类到 five_hour / weekly。

    不能按 primary/secondary 认窗口：实测多数账号只返回一个周窗口，而它落在
    primary 上；只有同时有 5h 和周限制时 primary 才是 5h。所以窗口种类只认
    limit_window_seconds，字段名仅决定遍历顺序。
    """
    found: dict[str, dict] = {}
    for key in ("primary", "secondary"):
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        try:
            seconds = float(window.get("window_seconds"))
        except (TypeError, ValueError):
            continue
        for kind, expected in _QUOTA_WINDOW_SECONDS.items():
            if kind not in found and abs(seconds - expected) <= _QUOTA_WINDOW_TOLERANCE:
                found[kind] = window
                break
    return found


def _is_zero_quota_payload(payload: dict, window_kind: str = "any") -> bool:
    """判断额度是否已耗尽，据此决定要不要排队入垃圾箱。

    ``window_kind``：``any`` 任一窗口耗尽即算（历史行为）；``five_hour`` /
    ``weekly`` 只看指定窗口。若上游没返回所选窗口，就回退到另一个可用窗口
    —— 只有周窗口的账号占绝大多数，严格按窗口判会让它们永远不再被回收。
    余额（credits_balance）耗尽与窗口无关，任何口径下都算耗尽。
    """
    if not isinstance(payload, dict) or payload.get("error_code"):
        return False
    credits = payload.get("credits_balance")
    try:
        if credits is not None and float(credits) <= 0:
            return True
    except Exception:
        pass
    kind = _normalize_trash_zero_quota_window(window_kind)
    if kind != "any":
        selected = _quota_windows_by_kind(payload).get(kind)
        if selected is not None:
            return _quota_window_is_exhausted(selected)
        # 所选窗口不存在：回退到任一窗口，宁可沿用旧口径也不要静默停掉回收。
    return any(
        _quota_window_is_exhausted(payload.get(key)) for key in ("primary", "secondary")
    )


def _quota_windows_exhausted(payload: dict, window_kind: str = "any") -> bool:
    """只看限流窗口，不看 credits 余额。

    自动重置要用这个而不是 :func:`_is_zero_quota_payload`：重置券只重置速率
    窗口，对 credits 余额耗尽无能为力，拿余额耗尽去兑券是纯浪费。
    """
    if not isinstance(payload, dict) or payload.get("error_code"):
        return False
    kind = _normalize_trash_zero_quota_window(window_kind)
    if kind != "any":
        selected = _quota_windows_by_kind(payload).get(kind)
        if selected is not None:
            return _quota_window_is_exhausted(selected)
        # 所选窗口不存在：回退到任一窗口，与入箱判定保持同一口径。
    return any(
        _quota_window_is_exhausted(payload.get(key)) for key in ("primary", "secondary")
    )


def _candidate_quota_auto_reset_enabled(workspace_id: int, settings: dict | None = None) -> bool:
    cfg = _workspace_settings_snapshot(workspace_id, settings)
    return bool(cfg.get("quota_auto_reset_enabled", False))


def _quota_auto_reset_blocked_reason(payload: dict, window_kind: str = "any") -> str:
    """这条额度记录现在兑券值不值？返回空字符串表示值得试。

    券是不可逆的消耗品，所以这里宁可放过也不要错兑。三种情况一律拦下：

    * ``workspace_member_credits_depleted`` —— 母号空间的池子被掏空，是空间级
      问题，重置券只重置速率窗口，兑了照样是 0；
    * 限流窗口没真的用尽（例如只是 credits 余额见底）—— 券用不上；
    * ``applicable`` 不是正数 —— 上游自己标注了"这张券现在不适用"。实测存在
      ``available=1, applicable=0`` 的账号，只看 available 就会白烧一张。
      ``applicable`` 缺失时同样拦下：宁可不自动兑，也不要按猜测消耗。
    """
    if not isinstance(payload, dict) or payload.get("error_code"):
        return "额度记录无效"
    if str(payload.get("rate_limit_reached_type") or "").strip() == "workspace_member_credits_depleted":
        return "空间额度耗尽，重置券只重置速率窗口，兑换无效"
    if not _quota_windows_exhausted(payload, window_kind):
        return "限流窗口未耗尽，重置券用不上"
    block = payload.get("reset_credits")
    if not isinstance(block, dict):
        return "上游未返回重置券信息"
    try:
        applicable = int(block.get("applicable"))
    except (TypeError, ValueError):
        return "上游未返回当前可用的重置券数"
    if applicable <= 0:
        return "当前没有可用的重置券"
    return ""


def _try_candidate_quota_auto_reset(
    workspace_id: int,
    email: str,
    quota: dict,
    settings: dict,
    *,
    quota_leases,
    source: str,
) -> dict | None:
    """额度耗尽时自动兑换一张重置券，返回兑换后重查到的额度；不兑则返回 None。

    调用方拿到非 None 才可以按"额度已恢复"处理，其余情况一律沿用原额度继续
    走入箱流程。兑换成功但重查失败时也返回 None —— 券确实花掉了，但没有证据
    说明额度回来了，此时按原计划排队入箱是安全的：入箱有延迟，到期复查会再看
    一次额度，真恢复了自然会被放行。

    这里吞掉所有异常：自动重置是"顺手救一把"，不能因为它失败就打断整轮额度
    任务或阻止入箱。
    """
    if not _candidate_quota_auto_reset_enabled(workspace_id, settings):
        return None
    window_kind = _candidate_trash_zero_quota_window(workspace_id, settings)
    reason = _quota_auto_reset_blocked_reason(quota, window_kind)
    if reason:
        logger.info(
            "自动重置跳过 workspace_db_id=%s email=%s source=%s reason=%s",
            workspace_id, email, source, reason,
        )
        return None
    try:
        consume_proxy = _lease_candidate_quota_proxy(
            quota_leases,
            workspace_id=workspace_id,
            email=email,
            detail=f"quota_auto_reset_{source}",
        )
        consumed = workspace_membership.consume_candidate_reset_credit(
            workspace_id, email, proxy=consume_proxy,
        )
    except workspace_membership.ResetCreditUnavailable as exc:
        # 落库的券数是上一次查询的快照，上游可能已经把券收走了。
        logger.warning(
            "自动重置无券可兑 workspace_db_id=%s email=%s source=%s error=%s",
            workspace_id, email, source, str(exc)[:200],
        )
        return None
    except Exception as exc:
        logger.warning(
            "自动重置兑换失败 workspace_db_id=%s email=%s source=%s error=%s",
            workspace_id, email, source, str(exc)[:200],
        )
        return None
    logger.warning(
        "自动重置已兑换重置券 workspace_db_id=%s email=%s source=%s credit_id=%s windows_reset=%s",
        workspace_id, email, source,
        consumed.get("credit_id"), consumed.get("windows_reset"),
    )
    try:
        refresh_proxy = _lease_candidate_quota_proxy(
            quota_leases,
            workspace_id=workspace_id,
            email=email,
            detail=f"quota_auto_reset_refresh_{source}",
        )
        return workspace_membership.fetch_candidate_quota(
            workspace_id,
            email,
            proxy=refresh_proxy,
            network_retries=_candidate_quota_network_retries(workspace_id, settings),
        )
    except Exception as exc:
        logger.warning(
            "自动重置后额度重查失败，按原额度继续 workspace_db_id=%s email=%s error=%s",
            workspace_id, email, str(exc)[:200],
        )
        return None


def _workspace_login_options(workspace_id: int, email: str, settings: dict, *, auto_export: bool = False) -> dict:
    master = db.get_workspace_master(workspace_id)
    if not master or not master.get("workspace_id"):
        raise HTTPException(400, "母号缺少 Workspace ID")
    proxy_pool = _candidate_proxy_pool_text(settings)
    if not proxy_pool:
        raise HTTPException(400, "候选人代理池为空")
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
        if not _workspace_exists(workspace_id):
            logging.getLogger("workspace_membership").info(
                "母号已删除，停止定时额度查询 workspace=%s", workspace_id
            )
            return
        settings = _workspace_settings_snapshot(workspace_id)
        if _workspace_automation_paused(settings):
            logging.getLogger("workspace_membership").info(
                "空间自动化已暂停，定时额度查询空转 workspace=%s", workspace_id
            )
            stop.wait(interval * 60)
            continue
        trash_delay = _candidate_trash_delay_seconds(workspace_id, settings)
        trash_window = _candidate_trash_zero_quota_window(workspace_id, settings)
        network_retries = _candidate_quota_network_retries(workspace_id, settings)
        candidates = [
            row for row in db.list_workspace_candidate_options(workspace_id)
            if not _candidate_quota_ineligible_reason(row)
        ]
        quota_leases = None
        if candidates:
            try:
                quota_leases = _candidate_quota_proxy_pool(
                    _candidate_proxy_pool_text(settings),
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
            if stop.is_set() or not _workspace_exists(workspace_id):
                return
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
                    network_retries=network_retries,
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
                                network_retries=network_retries,
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
            except workspace_membership.QuotaAccountDeactivated as exc:
                _handle_candidate_quota_deactivated(
                    workspace_id, email, settings, exc, source="quota_scheduled"
                )
                return
            except workspace_membership.QuotaPaymentRequired as exc:
                # 402 是母号空间级计费问题，会同时打到该空间下所有候选人，
                # 不能归因到单个账号，因此只告警不改任何账号状态。
                quota_logger.error(
                    "定时额度查询遇到空间计费异常 workspace=%s email=%s error=%s",
                    workspace_id,
                    email,
                    str(exc)[:300],
                )
                return
            except Exception as exc:
                quota_logger.warning(
                    "定时额度查询失败 workspace=%s email=%s error_type=%s status=%s error=%s",
                    workspace_id,
                    email,
                    type(exc).__name__,
                    getattr(exc, "status_code", ""),
                    str(exc)[:300],
                    exc_info=True,
                )
                return
            if stop.is_set() or not _workspace_exists(workspace_id):
                return
            if _is_zero_quota_payload(quota, trash_window):
                # 入箱前先给一次自救机会：兑券成功且额度真的回来了就不排队。
                refreshed = _try_candidate_quota_auto_reset(
                    workspace_id, email, quota, settings,
                    quota_leases=quota_leases, source="quota_scheduled",
                )
                if refreshed is not None:
                    quota = refreshed
            if _is_zero_quota_payload(quota, trash_window) and _candidate_trash_enabled(workspace_id, settings):
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


def _stop_workspace_task_schedulers(workspace_id: int) -> None:
    """Stop all background schedulers owned by a workspace before deletion."""
    workspace_id = int(workspace_id or 0)
    if not workspace_id:
        return
    # Mark first: a worker may already be processing a row while the delete
    # request is waiting on the database lock.
    _deleted_workspace_ids.add(workspace_id)
    stop_login_controllers_for_workspace(workspace_id)
    _stop_quota_scheduler(workspace_id)
    with _seat_auto_schedulers_lock:
        standard = _seat_auto_schedulers.pop(workspace_id, None)
    if standard:
        standard[0].set()
    with _prolite_auto_schedulers_lock:
        prolite = _prolite_auto_schedulers.pop(workspace_id, None)
    if prolite:
        prolite[0].set()


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
    候选操作、额度查询和导出直接复用。该操作只在用户手动点击“同步成员
    席位”时执行，自动补齐席位任务不会为此额外分页请求成员列表。
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
            if seat in {"default", "usage_based", "prolite"} and (gpt_seat or codex_seat) and member_id:
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


def _normalize_auto_prolite_candidate_seat_type(value: object) -> str:
    normalized = str(value or "default").strip().lower().replace("-", "_")
    if normalized in {"all", "any", "all_non_prolite", "全部", "全部席位"}:
        return "all"
    canonical = workspace_membership._canonical_candidate_seat_type(normalized)
    if canonical in {"default", "usage_based"}:
        return canonical
    return "default"


def _workspace_auto_prolite_candidates(
    workspace_id: int,
    seen: set[str] | None = None,
    candidate_seat_type: str = "default",
) -> list[dict]:
    """Return joined candidates eligible for ProLite upgrade.

    ``candidate_seat_type`` can target one source tier or ``all`` non-ProLite
    tiers.  The default remains the historical standard-seat behavior.
    """
    seen = seen or set()
    candidate_seat_type = _normalize_auto_prolite_candidate_seat_type(candidate_seat_type)
    rows = db.list_workspace_candidate_options(
        workspace_id,
        account_status="active",
        join_status="joined",
        seat_type="" if candidate_seat_type == "all" else candidate_seat_type,
        trash_status="active",
    )
    out = []
    for row in rows:
        email = str(row.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        if str(row.get("workspace_join_status") or "") != "joined":
            continue
        if str(row.get("account_status") or "active") == "permanently_invalid":
            continue
        if str(row.get("trash_status") or "active") == "trashed":
            continue
        seat = workspace_membership._canonical_candidate_seat_type(
            row.get("seat_label") or row.get("seat_type")
        )
        if candidate_seat_type == "all":
            if seat not in {"default", "usage_based"}:
                continue
        elif seat != candidate_seat_type:
            continue
        out.append(row)
    return out


_workspace_auto_advanced_candidates = _workspace_auto_prolite_candidates


def _workspace_seat_protect_exhausted(settings: dict) -> bool:
    if not settings.get("seat_protect_enabled"):
        return False
    threshold = max(1, int(settings.get("seat_protect_threshold") or 8))
    used = max(0, int(settings.get("seat_protect_used_count") or 0))
    return used >= threshold


def _workspace_prolite_seat_protect_exhausted(settings: dict) -> bool:
    if not settings.get("prolite_seat_protect_enabled"):
        return False
    threshold = max(1, int(settings.get("prolite_seat_protect_threshold") or 8))
    used = max(0, int(settings.get("prolite_seat_protect_used_count") or 0))
    return used >= threshold


def _switch_candidate_to_default_and_verify(workspace_id: int, candidate: dict, settings: dict) -> dict:
    email = str(candidate.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "error": "email 不能为空"}
    if not _workspace_exists(workspace_id):
        return {"ok": False, "error": "母号已删除"}
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
    if current_seat and current_seat not in {"default", "usage_based", "prolite"}:
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
        if not _workspace_exists(workspace_id):
            return {"ok": False, "email": email, "member_id": member_id, "error": "母号已删除"}
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
            try:
                db.increment_workspace_fulfillment_counter(workspace_id, "default", 1)
            except Exception:
                logger.exception("自动标准席位历史计数递增失败 workspace_db_id=%s email=%s", workspace_id, email)
            return {"ok": True, "email": email, "member_id": member_id, "seat": refreshed, "reserved": reserved}
        if attempt < 2:
            time.sleep(5)

    if reserved:
        try:
            db.release_workspace_seat_protect_quota(workspace_id, 1)
        except Exception:
            logger.exception("自动标准席位保护配额回滚失败 workspace_db_id=%s email=%s", workspace_id, email)
    return {"ok": False, "email": email, "member_id": member_id, "error": f"席位切换失败: {last_error or '复查未通过'}"}


def _switch_candidate_to_prolite_and_verify(
    workspace_id: int,
    candidate: dict,
    settings: dict | None = None,
) -> dict:
    """Switch one joined candidate to ProLite and verify the remote snapshot."""
    email = str(candidate.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "error": "email 不能为空"}
    if not _workspace_exists(workspace_id):
        return {"ok": False, "error": "母号已删除"}
    settings = settings or _workspace_settings_snapshot(workspace_id)
    row = db.get_workspace_candidate(workspace_id, email) or dict(candidate)
    if str(row.get("workspace_join_status") or "") != "joined":
        return {"ok": False, "error": "候选人尚未加入当前空间"}

    current_seat = workspace_membership.resolve_candidate_seat_type(workspace_id, email)
    if current_seat == "prolite":
        return {"ok": True, "skipped": True, "reason": "already_target_seat"}
    fresh = workspace_membership.fetch_candidate_seats(workspace_id, [email]).get(email, {})
    if not current_seat:
        current_seat = workspace_membership._canonical_candidate_seat_type(
            fresh.get("raw_seat_type") or fresh.get("seat_type") or fresh.get("seat_label")
        )
    if current_seat and current_seat not in {"default", "usage_based", "prolite"}:
        return {"ok": False, "error": f"当前席位不是可切换目标：{current_seat}"}
    member_id = str(fresh.get("member_id") or row.get("member_id") or "").strip()
    if not member_id:
        return {"ok": False, "error": "未获取到成员 member_id，请先校验候选状态"}

    reserved = False
    if settings.get("prolite_seat_protect_enabled"):
        reservation = db.reserve_workspace_prolite_seat_protect_quota(workspace_id, 1)
        if not reservation.get("allowed", False):
            return {
                "ok": False,
                "error": (
                    "高级席位保护已生效：本周期高级席位切换已达 "
                    f"{int(reservation.get('threshold') or settings.get('prolite_seat_protect_threshold') or 8)} 个，"
                    f"下次刷新时间 {reservation.get('refresh_time') or settings.get('prolite_seat_protect_refresh_time') or '00:00'}"
                ),
                "blocked_by_prolite_protect": True,
            }
        reserved = bool(reservation.get("enabled"))

    last_error: Exception | None = None
    for attempt in range(3):
        if not _workspace_exists(workspace_id):
            if reserved:
                db.release_workspace_prolite_seat_protect_quota(workspace_id, 1)
            return {"ok": False, "email": email, "member_id": member_id, "error": "母号已删除"}
        try:
            workspace_membership.update_member_seat_type(workspace_id, member_id, "prolite")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "自动高级席位切换请求失败 workspace_db_id=%s email=%s attempt=%s/3",
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
                "自动高级席位复查失败 workspace_db_id=%s email=%s attempt=%s/3",
                workspace_id,
                email,
                attempt + 1,
                exc_info=True,
            )
        seat = workspace_membership._canonical_candidate_seat_type(
            refreshed.get("raw_seat_type") or refreshed.get("seat_type") or refreshed.get("seat_label")
        )
        if seat == "prolite":
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
                "prolite",
            )
            try:
                db.increment_workspace_fulfillment_counter(workspace_id, "prolite", 1)
            except Exception:
                logger.exception("自动高级席位历史计数递增失败 workspace_db_id=%s email=%s", workspace_id, email)
            return {"ok": True, "email": email, "member_id": member_id, "seat": refreshed, "reserved": reserved}
        if attempt < 2:
            time.sleep(5)
    if reserved:
        try:
            db.release_workspace_prolite_seat_protect_quota(workspace_id, 1)
        except Exception:
            logger.exception("自动高级席位保护配额回滚失败 workspace_db_id=%s email=%s", workspace_id, email)
    return {"ok": False, "email": email, "member_id": member_id, "error": f"席位切换失败: {last_error or '复查未通过'}"}


_switch_candidate_to_advanced_and_verify = _switch_candidate_to_prolite_and_verify


def _enqueue_workspace_credentials(workspace_id: int, emails: list[str], settings: dict) -> dict:
    emails = [str(e).strip().lower() for e in emails if str(e).strip()]
    if not emails:
        return {"ok": True, "eligible": 0, "skipped": 0, "skipped_emails": []}
    master = db.get_workspace_master(workspace_id)
    if not master or not master.get("workspace_id"):
        raise RuntimeError("母号缺少 Workspace ID")
    proxy_pool = _candidate_proxy_pool_text(settings)
    if not proxy_pool:
        raise RuntimeError("候选人代理池为空")
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
        "target_count": 0,
    })
    if not result.get("ok") and "已经在跑了" not in str(result.get("error") or ""):
        raise RuntimeError(result.get("error") or "空间凭证任务启动失败")
    return result


def _auto_seat_interval_seconds(settings: dict | None) -> int:
    """Return the configured automatic seat replenishment polling period."""
    try:
        minutes = int((settings or {}).get("auto_seat_interval_minutes") or 5)
    except (TypeError, ValueError):
        minutes = 5
    return max(1, min(1440, minutes)) * 60


def _auto_seat_switch_gap_seconds(settings: dict | None) -> int:
    """串行补齐时两次成员席位切换之间的等待秒数。

    补齐永远是一次切一个（每轮重新拉一次上游席位数），这个间隔决定切换节奏；
    调大可以降低对上游席位接口的压力。0 表示不额外等待。
    """
    return db.normalize_gap_seconds((settings or {}).get("auto_seat_switch_gap_seconds"), 30)


def _auto_seat_target(settings: dict | None, key: str, entitled: int) -> int:
    """本轮补齐要补到的目标席位数。

    设置值 0（或缺失）表示跟随已购席位上限；正数作为固定目标，但仍钳制到
    不超过已购上限——上游只允许补到 entitled。
    """
    try:
        entitled = int(entitled or 0)
    except (TypeError, ValueError):
        entitled = 0
    try:
        configured = int((settings or {}).get(key) or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured <= 0:
        return entitled
    return min(entitled, configured)


def _auto_standard_seat_worker(workspace_id: int, stop: threading.Event):
    logger.info("自动标准席位任务启动 workspace_db_id=%s", workspace_id)
    try:
        while not stop.is_set():
            if not _workspace_exists(workspace_id):
                logger.info("母号已删除，停止自动标准席位任务 workspace_db_id=%s", workspace_id)
                return
            settings = _workspace_settings_snapshot(workspace_id)
            if _workspace_automation_paused(settings):
                logger.info("空间自动化已暂停，自动标准席位空转 workspace_db_id=%s", workspace_id)
                stop.wait(_auto_seat_interval_seconds(settings))
                continue
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
                stop.wait(_auto_seat_interval_seconds(settings))
                continue
            seat_info = _refresh_workspace_seat_info(workspace_id, retries=3, delay_seconds=5)
            if not seat_info:
                stop.wait(_auto_seat_interval_seconds(settings))
                continue
            try:
                db.update_workspace_seat_info(workspace_id, **seat_info)
            except Exception:
                logger.exception("自动标准席位刷新写回母号失败 workspace_db_id=%s", workspace_id)
            entitled = int(
                seat_info.get("seats_default_entitled")
                if seat_info.get("seats_default_entitled") is not None
                else (seat_info.get("seats_entitled") or 0)
            )
            current_default = int(seat_info.get("seats_default") or 0)
            target = _auto_seat_target(settings, "auto_standard_seat_target", entitled)
            if target <= 0 or current_default >= target:
                stop.wait(_auto_seat_interval_seconds(settings))
                continue

            switched: list[str] = []
            attempted: set[str] = set()
            while not stop.is_set():
                if not _workspace_exists(workspace_id):
                    logger.info("母号已删除，停止自动标准席位任务 workspace_db_id=%s", workspace_id)
                    break
                settings = _workspace_settings_snapshot(workspace_id)
                if _workspace_automation_paused(settings):
                    logger.info("空间自动化已暂停，自动标准席位停止本轮 workspace_db_id=%s", workspace_id)
                    break
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
                entitled = int(
                    seat_info.get("seats_default_entitled")
                    if seat_info.get("seats_default_entitled") is not None
                    else (seat_info.get("seats_entitled") or 0)
                )
                current_default = int(seat_info.get("seats_default") or 0)
                deficit = max(0, _auto_seat_target(settings, "auto_standard_seat_target", entitled) - current_default)
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
                # 串行切换：本轮的 settings 每次循环开头都重新取，改间隔立即生效。
                stop.wait(_auto_seat_switch_gap_seconds(settings))

            if switched:
                if not _workspace_exists(workspace_id):
                    switched.clear()
                else:
                    try:
                        _enqueue_workspace_credentials(workspace_id, switched, _workspace_settings_snapshot(workspace_id))
                    except Exception:
                        logger.exception("自动标准席位后续凭证获取失败 workspace_db_id=%s emails=%s", workspace_id, switched)
            stop.wait(_auto_seat_interval_seconds(_workspace_settings_snapshot(workspace_id)))
    finally:
        with _seat_auto_schedulers_lock:
            current = _seat_auto_schedulers.get(workspace_id)
            if current and current[0] is stop:
                _seat_auto_schedulers.pop(workspace_id, None)
        logger.info("自动标准席位任务结束 workspace_db_id=%s", workspace_id)


def _auto_prolite_seat_worker(workspace_id: int, stop: threading.Event):
    """Poll ProLite capacity and upgrade joined standard-seat candidates."""
    logger.info("自动高级席位任务启动 workspace_db_id=%s", workspace_id)
    try:
        while not stop.is_set():
            if not _workspace_exists(workspace_id):
                logger.info("母号已删除，停止自动高级席位任务 workspace_db_id=%s", workspace_id)
                return
            settings = _workspace_settings_snapshot(workspace_id)
            if _workspace_automation_paused(settings):
                logger.info("空间自动化已暂停，自动高级席位空转 workspace_db_id=%s", workspace_id)
                stop.wait(_auto_seat_interval_seconds(settings))
                continue
            if not settings.get("auto_prolite_seat_enabled"):
                logger.info("自动高级席位任务已关闭 workspace_db_id=%s", workspace_id)
                return
            if _workspace_prolite_seat_protect_exhausted(settings):
                logger.info(
                    "自动高级席位任务因高级席位保护跳过本轮 workspace_db_id=%s used=%s threshold=%s",
                    workspace_id,
                    int(settings.get("prolite_seat_protect_used_count") or 0),
                    int(settings.get("prolite_seat_protect_threshold") or 8),
                )
                stop.wait(_auto_seat_interval_seconds(settings))
                continue
            seat_info = _refresh_workspace_seat_info(workspace_id, retries=3, delay_seconds=5)
            if not seat_info:
                stop.wait(_auto_seat_interval_seconds(settings))
                continue
            try:
                db.update_workspace_seat_info(workspace_id, **seat_info)
            except Exception:
                logger.exception("自动高级席位刷新写回母号失败 workspace_db_id=%s", workspace_id)
            entitled = seat_info.get("seats_prolite_entitled")
            current_prolite = seat_info.get("seats_prolite")
            # ProLite 容量必须使用专属 entitlement；缺失时不能误把总席位当作高级席位容量。
            if entitled is None or current_prolite is None:
                stop.wait(_auto_seat_interval_seconds(settings))
                continue
            entitled = int(entitled or 0)
            current_prolite = int(current_prolite or 0)
            target = _auto_seat_target(settings, "auto_prolite_seat_target", entitled)
            if target <= 0 or current_prolite >= target:
                stop.wait(_auto_seat_interval_seconds(settings))
                continue

            switched: list[str] = []
            attempted: set[str] = set()
            while not stop.is_set():
                if not _workspace_exists(workspace_id):
                    logger.info("母号已删除，停止自动高级席位任务 workspace_db_id=%s", workspace_id)
                    break
                settings = _workspace_settings_snapshot(workspace_id)
                if _workspace_automation_paused(settings):
                    logger.info("空间自动化已暂停，自动高级席位停止本轮 workspace_db_id=%s", workspace_id)
                    break
                if _workspace_prolite_seat_protect_exhausted(settings):
                    logger.info(
                        "自动高级席位任务达到高级席位保护阈值，停止本轮 workspace_db_id=%s used=%s threshold=%s",
                        workspace_id,
                        int(settings.get("prolite_seat_protect_used_count") or 0),
                        int(settings.get("prolite_seat_protect_threshold") or 8),
                    )
                    break
                seat_info = _refresh_workspace_seat_info(workspace_id, retries=3, delay_seconds=5)
                if not seat_info:
                    break
                try:
                    db.update_workspace_seat_info(workspace_id, **seat_info)
                except Exception:
                    logger.exception("自动高级席位刷新写回母号失败 workspace_db_id=%s", workspace_id)
                entitled = seat_info.get("seats_prolite_entitled")
                current_prolite = seat_info.get("seats_prolite")
                if entitled is None or current_prolite is None:
                    break
                deficit = max(0, _auto_seat_target(settings, "auto_prolite_seat_target", entitled) - int(current_prolite or 0))
                if deficit <= 0:
                    break
                candidates = _workspace_auto_prolite_candidates(
                    workspace_id,
                    attempted,
                    settings.get("auto_prolite_candidate_seat_type", "default"),
                )
                if not candidates:
                    logger.info(
                        "自动高级席位任务候选不足 workspace_db_id=%s deficit=%s",
                        workspace_id,
                        deficit,
                    )
                    break
                candidate = candidates[0]
                email = str(candidate.get("email") or "").strip().lower()
                attempted.add(email)
                result = _switch_candidate_to_prolite_and_verify(workspace_id, candidate, settings)
                if result.get("ok") and not result.get("skipped"):
                    switched.append(email)
                    logger.info("自动高级席位切换成功 workspace_db_id=%s email=%s", workspace_id, email)
                elif result.get("skipped"):
                    logger.info("自动高级席位候选已是目标席位 workspace_db_id=%s email=%s", workspace_id, email)
                elif result.get("blocked_by_prolite_protect"):
                    logger.info(
                        "自动高级席位任务受到高级席位保护，停止本轮 workspace_db_id=%s email=%s",
                        workspace_id,
                        email,
                    )
                    break
                else:
                    logger.warning(
                        "自动高级席位切换失败 workspace_db_id=%s email=%s error=%s",
                        workspace_id,
                        email,
                        result.get("error"),
                    )
                if stop.is_set():
                    break
                # 串行切换：本轮的 settings 每次循环开头都重新取，改间隔立即生效。
                stop.wait(_auto_seat_switch_gap_seconds(settings))

            if switched:
                if not _workspace_exists(workspace_id):
                    switched.clear()
                else:
                    try:
                        _enqueue_workspace_credentials(workspace_id, switched, _workspace_settings_snapshot(workspace_id))
                    except Exception:
                        logger.exception("自动高级席位后续凭证获取失败 workspace_db_id=%s emails=%s", workspace_id, switched)
            stop.wait(_auto_seat_interval_seconds(_workspace_settings_snapshot(workspace_id)))
    finally:
        with _prolite_auto_schedulers_lock:
            current = _prolite_auto_schedulers.get(workspace_id)
            if current and current[0] is stop:
                _prolite_auto_schedulers.pop(workspace_id, None)
        logger.info("自动高级席位任务结束 workspace_db_id=%s", workspace_id)


# Internal compatibility alias: ProLite is the API's canonical name for the
# product's “advanced seat” tier.
_auto_advanced_seat_worker = _auto_prolite_seat_worker


_trash_sweeper_stop = threading.Event()
_trash_sweeper_thread: threading.Thread | None = None


def _reconcile_invalid_candidate_trash(limit: int = 500) -> dict:
    rows = db.list_invalid_workspace_candidates_pending_trash(limit=limit)
    settings_by_workspace: dict[int, dict] = {}
    marked = 0
    skipped = 0
    seat_pending = 0
    # 失效补偿一样是串行的，且每条都会打上游席位切换接口；在上一条真的入过箱之后
    # 等一个间隔再处理下一条，与到期入箱共用同一个设置。跳过的行不占用间隔。
    last_row_trashed = False
    for row in rows:
        workspace_id = int(row.get("workspace_master_id") or 0)
        email = str(row.get("email") or "").strip().lower()
        if not workspace_id or not email:
            continue
        if not _workspace_exists(workspace_id):
            skipped += 1
            continue
        settings = settings_by_workspace.get(workspace_id)
        if settings is None:
            settings = db.get_workspace_settings(workspace_id)
            settings_by_workspace[workspace_id] = settings
        if _workspace_automation_paused(settings):
            skipped += 1
            continue
        if not _candidate_trash_invalid_enabled(workspace_id, settings):
            skipped += 1
            continue
        # 没有成员关系就无法完成远端席位切换；这类记录不能仅凭账号
        # 失效状态直接标记为已入箱。
        if not row.get("member_id") and str(row.get("workspace_join_status") or "") != "joined":
            skipped += 1
            continue
        gap = _candidate_trash_gap_seconds(workspace_id, settings)
        if last_row_trashed and gap:
            logger.info(
                "失效候选人串行入箱等待 workspace_db_id=%s email=%s gap=%ss",
                workspace_id,
                email,
                gap,
            )
            if _trash_sweeper_stop.wait(gap):
                break
        last_row_trashed = True
        try:
            # trash_workspace_candidate 内部先切换并远端复查 usage_based，只有
            # 复查成功后才会把 trash_status 写成 trashed；这里绝不提前标记。
            result = workspace_membership.trash_workspace_candidate(
                workspace_id,
                email,
                reason="account_invalid",
                retries=1,
            )
            if result.get("ok"):
                marked += 1
            elif result.get("pending_seat"):
                seat_pending += 1
            else:
                skipped += 1
        except Exception:
            seat_pending += 1
            logger.exception(
                "失效候选人切换 Codex 后入箱失败 workspace_db_id=%s email=%s",
                workspace_id,
                email,
            )
    return {
        "scanned": len(rows),
        "marked": marked,
        "skipped": skipped,
        "seat_pending": seat_pending,
    }


def _trash_sweeper_worker():
    while not _trash_sweeper_stop.is_set():
        try:
            reconciled = _reconcile_invalid_candidate_trash()
            if reconciled["marked"]:
                logger.info("失效候选人自动入箱补偿完成 result=%s", reconciled)
            quota_lease_pools: dict[int, public_relogin.ProxyLeasePool] = {}
            # 到期行本来就是一条一条处理的；多条同时到期时，在上一条真的入箱之后
            # 等一个间隔再处理下一条，给连着打上游席位切换接口限速。只在"刚入过箱"
            # 之后等待：否则一轮里若只有开头入箱、后面几十条都是复查放行，整轮会
            # 被白等拖垮。
            last_row_trashed = False
            for row in db.list_workspace_candidate_trash_due():
                if _trash_sweeper_stop.is_set():
                    break
                workspace_id = int(row.get("workspace_master_id") or 0)
                if not workspace_id:
                    continue
                if not _workspace_exists(workspace_id):
                    # A row may have been fetched just before the workspace
                    # deletion committed.  Do not issue any follow-up quota,
                    # relogin, or seat-switch request for that stale row.
                    continue
                settings = _workspace_settings_snapshot(workspace_id)
                if _workspace_automation_paused(settings):
                    continue
                gap = _candidate_trash_gap_seconds(workspace_id, settings)
                if last_row_trashed and gap:
                    logger.info(
                        "垃圾箱串行入箱等待 workspace_db_id=%s email=%s gap=%ss",
                        workspace_id,
                        row.get("email", ""),
                        gap,
                    )
                    if _trash_sweeper_stop.wait(gap):
                        break
                last_row_trashed = False
                try:
                    quota_leases = quota_lease_pools.get(workspace_id)
                    if quota_leases is None:
                        try:
                            quota_leases = _candidate_quota_proxy_pool(_candidate_proxy_pool_text(settings))
                        except ValueError:
                            # 交给单条处理函数统一记录失败并把复查时间后移，避免
                            # 到期行每 30 秒被 sweeper 反复捞起。
                            last_row_trashed = _process_scheduled_trash_due(row, settings)
                            continue
                        quota_lease_pools[workspace_id] = quota_leases
                    last_row_trashed = _process_scheduled_trash_due(row, settings, quota_leases)
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
                    if workspace_id not in _seat_auto_schedulers:
                        stop = threading.Event()
                        thread = threading.Thread(
                            target=_auto_standard_seat_worker,
                            args=(workspace_id, stop),
                            daemon=True,
                            name=f"seat-auto-{workspace_id}",
                        )
                        next_at = time.time() + _auto_seat_interval_seconds(settings)
                        _seat_auto_schedulers[workspace_id] = (stop, thread, next_at)
                        thread.start()
            if settings.get("auto_prolite_seat_enabled"):
                with _prolite_auto_schedulers_lock:
                    if workspace_id not in _prolite_auto_schedulers:
                        stop = threading.Event()
                        thread = threading.Thread(
                            target=_auto_prolite_seat_worker,
                            args=(workspace_id, stop),
                            daemon=True,
                            name=f"seat-auto-prolite-{workspace_id}",
                        )
                        next_at = time.time() + _auto_seat_interval_seconds(settings)
                        _prolite_auto_schedulers[workspace_id] = (stop, thread, next_at)
                        thread.start()
    except Exception:
        logger.exception("启动时恢复自动席位任务失败")


@app.on_event("shutdown")
def _stop_background_sweeper():
    _trash_sweeper_stop.set()
    with _quota_schedulers_lock:
        quota_items = list(_quota_schedulers.values())
        _quota_schedulers.clear()
    for item in quota_items:
        item[0].set()
    with _seat_auto_schedulers_lock:
        seat_items = list(_seat_auto_schedulers.values())
        _seat_auto_schedulers.clear()
    for item in seat_items:
        item[0].set()
    with _prolite_auto_schedulers_lock:
        prolite_items = list(_prolite_auto_schedulers.values())
        _prolite_auto_schedulers.clear()
    for item in prolite_items:
        item[0].set()


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    group_name: str = Field("", description="邮箱分组；空=未分组，__all__=全部")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    want_password: bool = True  # 新账号是否强制创建可长期登录的密码
    proxy: str = ""
    proxy_pool: str = Field("", description="系统代理池；单次注册未指定 proxy 时按最少租用次数选择")
    otp_timeout: int = 10
    add_phone_mode: str = Field("api", description="add-phone 验证模式：api / camoufox")
    register_mode: str = Field("protocol", description="注册流程：protocol / camoufox")
    debug_mode: bool = Field(False, description="Camoufox 调试模式：失败时保存页面截图")
    allow_existing_login: bool = True
    # 服务端识别邮箱已存在时，是否切换到登录并补齐缺失 2FA；留空时
    # 跟随 want_2fa 页面开关推导。已有账号必须已有密码，系统不会创建密码。
    ensure_credentials: Optional[bool] = None
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
        result = db.import_accounts(
            req.text, kind=req.kind, group_name=req.group_name,
            relay_suffix=req.relay_suffix,
        )
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


@app.post("/api/registered/import_2fa")
def api_import_2fa(req: Import2FAReq):
    """导入 邮箱----密码----2FA 格式的已注册账号，直接写入注册结果表。"""
    try:
        result = db.import_2fa_registered(req.text, group_name=req.group_name)
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
    ids = sorted({int(item) for item in (req.ids or []) if int(item) > 0})
    existing_ids = [workspace_id for workspace_id in ids if db.get_workspace_master(workspace_id)]
    for workspace_id in existing_ids:
        _stop_workspace_task_schedulers(workspace_id)
    return {"ok": True, "deleted": db.delete_workspace_masters(ids)}


@app.get("/api/workspaces/{workspace_id}")
def api_workspace_master(workspace_id: int):
    row = db.get_workspace_master(workspace_id)
    if not row:
        raise HTTPException(404, "母号不存在")
    return {"ok": True, "data": row}


@app.delete("/api/workspaces/{workspace_id}")
def api_delete_workspace_master(workspace_id: int):
    _stop_workspace_task_schedulers(workspace_id)
    if not db.delete_workspace_master(workspace_id):
        _deleted_workspace_ids.discard(int(workspace_id))
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


def _candidate_trash_gap_seconds(workspace_id: int, settings: dict | None = None) -> int:
    """同一轮回收里两次入箱之间的等待秒数。

    到期入箱本来就是串行的（sweeper 一行一行处理），但多个候选人常常在相近时间
    到期，入箱又要连着打上游的席位切换接口。这个间隔给连续入箱限速；0 表示不等待。
    """
    cfg = _workspace_settings_snapshot(workspace_id, settings)
    return db.normalize_gap_seconds(cfg.get("trash_gap_seconds"), 30)


def _candidate_quota_network_retries(workspace_id: int, settings: dict | None = None) -> int:
    cfg = _workspace_settings_snapshot(workspace_id, settings)
    try:
        value = int(cfg.get("quota_network_retries", 2))
    except (TypeError, ValueError):
        value = 2
    return max(0, min(5, value))


def _handle_candidate_quota_deactivated(
    workspace_id: int,
    email: str,
    settings: dict | None,
    exc: Exception,
    *,
    source: str,
) -> None:
    """连续 403 判定停用后的统一收尾：标记账号失效并按设置入箱。

    与 ``_wait_and_relogin_for_candidate`` 里的失效处理走同一套动作，保证
    无论从哪条路径识别出停用，账号与候选关系的落库结果一致。
    """
    logger.warning(
        "额度查询判定账号停用 source=%s workspace_db_id=%s email=%s streak=%s error=%s",
        source,
        workspace_id,
        email,
        getattr(exc, "streak", 0),
        str(exc)[:200],
    )
    try:
        db.mark_registered_permanently_invalid(email, reason="quota_403")
    except Exception:
        logger.exception("账号停用标记失败 workspace_db_id=%s email=%s", workspace_id, email)
    if not _candidate_trash_invalid_enabled(workspace_id, settings):
        return
    try:
        workspace_membership.trash_workspace_candidates_by_email(
            email,
            reason="quota_403",
            respect_invalid_settings=True,
        )
    except Exception:
        logger.exception("账号停用后垃圾箱处理失败 workspace_db_id=%s email=%s", workspace_id, email)


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
    result = workspace_membership.trash_workspace_candidate(workspace_id, email, reason=reason)
    if not result.get("ok") and result.get("pending_seat"):
        db.update_workspace_candidate_trash(
            workspace_id,
            email,
            status="scheduled",
            due_at=time.time() + 10 * 60,
            reason="seat_retry",
        )
        logger.warning(
            "垃圾箱席位切换已延期 workspace_db_id=%s email=%s retry_at=10m",
            workspace_id, email,
        )
    return result


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
                workspace_membership.trash_workspace_candidates_by_email(
                    email,
                    reason="login_403",
                    respect_invalid_settings=True,
                )
            except Exception:
                logger.exception("账号失效后垃圾箱处理失败 workspace_db_id=%s email=%s", workspace_id, email)
        return False
    return True


def _process_scheduled_trash_due(
    row: dict,
    settings: dict,
    quota_leases: public_relogin.ProxyLeasePool | None = None,
) -> bool:
    """复查一条到期记录，返回是否真的执行了入箱（含失败的入箱尝试）。

    返回值给 sweeper 用来在连续入箱之间限速：只有真正打了上游席位接口的行才
    需要间隔，纯复查后放行的行不该拖慢整轮。
    """
    workspace_id = int(row.get("workspace_master_id") or 0)
    email = str(row.get("email") or "").strip().lower()
    if not workspace_id or not email:
        return False
    if not _workspace_exists(workspace_id):
        return False
    if not _candidate_trash_enabled(workspace_id, settings):
        _clear_candidate_trash_timer(workspace_id, email)
        return False
    quota_proxy = ""
    try:
        if not _workspace_exists(workspace_id):
            return
        if quota_leases is None:
            quota_leases = _candidate_quota_proxy_pool(_candidate_proxy_pool_text(settings))
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
            network_retries=_candidate_quota_network_retries(workspace_id, settings),
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
                    network_retries=_candidate_quota_network_retries(workspace_id, settings),
                )
            else:
                if not _candidate_trash_invalid_enabled(workspace_id, settings):
                    _clear_candidate_trash_timer(workspace_id, email)
                    return False
                # 重登录失败已在内部按失效入箱，算作一次真实入箱动作。
                return True
        except Exception:
            logger.exception("垃圾箱到期复查 401 后重试失败 workspace_db_id=%s email=%s", workspace_id, email)
            db.update_workspace_candidate_trash(
                workspace_id,
                email,
                status="scheduled",
                due_at=time.time() + 10 * 60,
                reason="quota_401_retry",
            )
            return False
    except workspace_membership.QuotaAccountDeactivated as exc:
        # 停用判定本身就是终态结论，直接走失效入箱，不再顺延复查。
        _handle_candidate_quota_deactivated(
            workspace_id, email, settings, exc, source="trash_recheck"
        )
        return True
    except workspace_membership.QuotaPaymentRequired as exc:
        logger.error(
            "垃圾箱到期复查遇到空间计费异常 workspace_db_id=%s email=%s error=%s",
            workspace_id,
            email,
            str(exc)[:300],
        )
        db.update_workspace_candidate_trash(
            workspace_id,
            email,
            status="scheduled",
            due_at=time.time() + 10 * 60,
            reason="quota_402_retry",
        )
        return False
    except Exception:
        logger.exception("垃圾箱到期额度复查失败 workspace_db_id=%s email=%s", workspace_id, email)
        db.update_workspace_candidate_trash(
            workspace_id,
            email,
            status="scheduled",
            due_at=time.time() + 10 * 60,
            reason="quota_retry",
        )
        return False
    if not _workspace_exists(workspace_id):
        return False
    trash_window = _candidate_trash_zero_quota_window(workspace_id, settings)
    if _is_zero_quota_payload(quota, trash_window):
        # 真要入箱之前的最后一次自救：这时账号已经躺了一个延迟周期，上游可能
        # 刚发了新券。兑换失败或额度没回来都照常入箱。
        refreshed = _try_candidate_quota_auto_reset(
            workspace_id, email, quota, settings,
            quota_leases=quota_leases, source="trash_recheck",
        )
        if refreshed is not None:
            quota = refreshed
    if _is_zero_quota_payload(quota, trash_window):
        try:
            _apply_candidate_trash(workspace_id, email, reason="quota_zero")
        except Exception:
            logger.exception("垃圾箱入箱执行失败，将在 10 分钟后重试 workspace_db_id=%s email=%s", workspace_id, email)
            db.update_workspace_candidate_trash(
                workspace_id,
                email,
                status="scheduled",
                due_at=time.time() + 10 * 60,
                reason="trash_retry",
            )
        return True
    _clear_candidate_trash_timer(workspace_id, email)
    return False


@app.post("/api/workspace-candidates/kick")
def api_kick_workspace_candidates(req: WorkspaceCandidatesReq):
    """把候选人从 OpenAI 空间移除：上游 DELETE users/{member_id}，成功后在本地清成员身份。"""
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    indexed = _workspace_candidate_index(req.workspace_id)
    emails = list(dict.fromkeys(
        email.strip().lower()
        for email in req.emails
        if email.strip() and email.strip().lower() in indexed
    ))
    if not emails:
        raise HTTPException(400, "所选账号不是当前母号空间的候选人")
    # 踢人节奏：每踢完一个在 [min, max] 范围内随机 sleep，降低对上游
    # 管理接口的突发压力；两个值都为 0 时不等待。
    kick_settings = db.get_workspace_settings(req.workspace_id)
    kick_delay_min = db.normalize_gap_seconds(
        kick_settings.get("kick_delay_min_seconds"), 2,
    )
    kick_delay_max = db.normalize_gap_seconds(
        kick_settings.get("kick_delay_max_seconds"), 5,
    )
    if kick_delay_max < kick_delay_min:
        kick_delay_min, kick_delay_max = kick_delay_max, kick_delay_min
    results = []
    for index, email in enumerate(emails):
        row = indexed.get(email) or {}
        member_id = str(row.get("member_id") or "").strip()
        try:
            result = workspace_membership.remove_member(req.workspace_id, email, member_id)
            ok = bool(result.get("kicked") or result.get("already_gone"))
            if ok:
                # 人已离开空间：移入本空间垃圾箱并标记已踢出（清空成员身份/席位、
                # 删除空间凭证），注册结果/号池保留，可从垃圾箱恢复或彻底删除。
                db.mark_workspace_candidates_kicked(req.workspace_id, [email])
                if index < len(emails) - 1 and kick_delay_max > 0:
                    delay = random.uniform(kick_delay_min, kick_delay_max)
                    logger.info(
                        "踢出空间成员节流 workspace_db_id=%s email=%s sleep=%.1fs",
                        req.workspace_id, email, delay,
                    )
                    time.sleep(delay)
            results.append({
                "email": email,
                "ok": ok,
                "result": result,
                "error": "" if ok else result.get("error") or "未找到空间成员记录",
            })
        except Exception as exc:
            logger.exception("踢出空间成员失败 workspace_db_id=%s email=%s", req.workspace_id, email)
            results.append({"email": email, "ok": False, "error": str(exc)})
    return {
        "ok": not any(not item.get("ok") for item in results),
        "results": results,
        "kicked": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
    }


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
            result = workspace_membership.trash_workspace_candidate(
                req.workspace_id,
                email,
                reason="manual_trash",
            )
            results.append({
                "email": email,
                "ok": bool(result.get("ok")),
                "result": result,
                "error": result.get("error", ""),
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


@app.post("/api/workspace-candidates/trash/restore")
def api_restore_workspace_candidates_from_trash(req: WorkspaceCandidatesReq):
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    indexed = _workspace_candidate_index(req.workspace_id)
    emails = list(dict.fromkeys(
        email.strip().lower()
        for email in req.emails
        if email.strip() and email.strip().lower() in indexed
    ))
    if not emails:
        raise HTTPException(400, "所选账号不是当前母号空间的候选人")
    trashed = [email for email in emails if indexed[email].get("trash_status") == "trashed"]
    restored = db.restore_workspace_candidates_from_trash(req.workspace_id, trashed)
    return {
        "ok": True,
        "restored": restored,
        "skipped": len(emails) - restored,
    }


@app.post("/api/workspace-candidates/trash/empty")
def api_empty_workspace_trash(req: WorkspaceCandidatesReq):
    """清空垃圾箱：把该空间垃圾箱内全部账号从整个系统删除。"""
    emails = db.list_trashed_workspace_candidate_emails(req.workspace_id)
    if not emails:
        return {"ok": True, "deleted": 0}
    counts = db.delete_account_everywhere(emails)
    return {"ok": True, "deleted": len(emails), **counts}


def _lease_reset_credit_proxy(req: WorkspaceResetCreditReq, email: str, detail: str) -> str:
    """重置券操作的代理：与额度查询同源，不允许回退到母号出口。"""
    settings = _workspace_settings_snapshot(req.workspace_id)
    try:
        leases = _candidate_quota_proxy_pool(
            _candidate_proxy_pool_text(settings, req.proxy_pool)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _lease_candidate_quota_proxy(
        leases, workspace_id=req.workspace_id, email=email, detail=detail,
    )


def _reset_credit_target(req: WorkspaceResetCreditReq) -> str:
    """校验邮箱属于本空间且允许做额度类操作，返回归一化后的邮箱。"""
    email = str(req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "请选择候选人")
    row = db.get_workspace_candidate(req.workspace_id, email) or {}
    if not row:
        raise HTTPException(400, "所选账号不是当前母号空间的候选人")
    # 复用额度查询那套准入判定：没凭证/已出库/已入箱的账号同样不该动券。
    # 注意 list_workspace_candidate_options 会过滤掉已出库/已入箱的行，所以
    # 候选人自身的字段要盖在上面——否则这两类会被误报成"不属于当前空间"。
    options = {
        str(item.get("email") or "").strip().lower(): item
        for item in db.list_workspace_candidate_options(req.workspace_id)
    }
    reason = _candidate_quota_ineligible_reason(
        {**(options.get(email) or {}), **row}, strict_seat=False
    )
    if reason:
        raise HTTPException(400, reason)
    return email


@app.get("/api/workspace-candidates/reset-credits")
def api_list_candidate_reset_credits(workspace_id: int, email: str, proxy_pool: str = ""):
    """只读列出候选人名下的重置券，供兑换前确认。"""
    req = WorkspaceResetCreditReq(workspace_id=workspace_id, email=email, proxy_pool=proxy_pool)
    target = _reset_credit_target(req)
    proxy = _lease_reset_credit_proxy(req, target, "reset_credit_list")
    try:
        listing = workspace_membership.list_candidate_reset_credits(
            workspace_id, target, proxy=proxy,
        )
    except Exception as exc:
        logger.exception("重置券查询失败 workspace_db_id=%s email=%s", workspace_id, target)
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "email": target, **listing}


@app.post("/api/workspace-candidates/reset-credits/consume")
def api_consume_candidate_reset_credit(req: WorkspaceResetCreditReq):
    """兑换一张重置券，然后立刻重查额度把结果写回。

    兑换本身不可逆，所以这里不重试。兑换成功后的额度重查是"锦上添花"：失败
    也只报告查询错误，绝不能让它看起来像兑换失败，否则用户会再点一次、再烧
    掉一张券。
    """
    target = _reset_credit_target(req)
    proxy = _lease_reset_credit_proxy(req, target, "reset_credit_consume")
    try:
        consumed = workspace_membership.consume_candidate_reset_credit(
            req.workspace_id, target, proxy=proxy, credit_id=req.credit_id,
        )
    except workspace_membership.ResetCreditUnavailable as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("重置券兑换失败 workspace_db_id=%s email=%s", req.workspace_id, target)
        raise HTTPException(400, str(exc)) from exc

    settings = _workspace_settings_snapshot(req.workspace_id)
    quota = None
    quota_error = ""
    try:
        refresh_proxy = _lease_reset_credit_proxy(req, target, "reset_credit_refresh")
        quota = workspace_membership.fetch_candidate_quota(
            req.workspace_id, target, proxy=refresh_proxy,
            network_retries=_candidate_quota_network_retries(req.workspace_id, settings),
        )
    except Exception as exc:
        quota_error = str(exc)
        logger.warning(
            "重置券兑换成功但额度重查失败 workspace_db_id=%s email=%s error=%s",
            req.workspace_id, target, quota_error[:200],
        )
    return {
        "ok": True,
        "email": target,
        "consumed": consumed,
        "quota": quota,
        "quota_error": quota_error,
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
    group_name: str = "",
    tag: str = "",
    redeem_status: str = "",
    quota_status: str = "",
    keyword: str = "",
):
    limit = max(1, min(1000, int(limit or 100)))
    offset = max(0, int(offset or 0))
    items = db.list_workspace_candidate_options(
        workspace_id, limit=limit, offset=offset,
        account_status=account_status, join_status=join_status,
        credential_status=credential_status, seat_type=seat_type,
        trash_status=trash_status, tag_status=tag_status,
        group_name=group_name, tag=tag, redeem_status=redeem_status,
        quota_status=quota_status,
        keyword=keyword,
    )
    for item in items:
        # 行上的可查询标记按手动口径判定：席位未同步只挡自动任务，不挡手动查询。
        reason = _candidate_quota_ineligible_reason(item, strict_seat=False)
        item["quota_eligible"] = not reason
        item["quota_ineligible_reason"] = reason
    total = db.count_workspace_candidate_options(
        workspace_id, account_status=account_status, join_status=join_status,
        credential_status=credential_status, seat_type=seat_type,
        trash_status=trash_status, tag_status=tag_status,
        group_name=group_name, tag=tag, redeem_status=redeem_status,
        quota_status=quota_status,
        keyword=keyword,
    )
    stats = db.get_workspace_candidate_stats(workspace_id)
    return {"ok": True, "items": items, "total": total, "limit": limit, "offset": offset, "stats": stats}


@app.get("/api/workspace-candidates/stats")
def api_workspace_candidate_stats(workspace_id: int):
    return {"ok": True, "stats": db.get_workspace_candidate_stats(workspace_id)}


@app.get("/api/workspace-candidates/groups")
def api_workspace_candidate_groups(workspace_id: int):
    return {"ok": True, "groups": db.list_workspace_candidate_groups(workspace_id)}


@app.get("/api/workspace-candidates/tags")
def api_workspace_candidate_tags(workspace_id: int):
    return {"ok": True, "tags": db.list_workspace_candidate_tags(workspace_id)}


@app.post("/api/workspace-candidates/tags")
def api_set_workspace_candidate_tags(req: WorkspaceCandidatesReq):
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    try:
        changed = db.set_workspace_candidate_tags(
            req.workspace_id, req.emails, req.tags, mode=req.tag_mode,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "changed": changed}


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


@app.post("/api/workspace-candidates/delete-everywhere")
def api_delete_workspace_candidates_everywhere(req: WorkspaceCandidatesReq):
    """删除账号：所有空间的候选划分和空间凭证、注册结果、号池行一起清掉。"""
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    counts = db.delete_account_everywhere(req.emails)
    return {"ok": True, "deleted": len(req.emails), **counts}


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
    if req.seat_type not in {"default", "usage_based", "prolite"}:
        raise HTTPException(400, "席位类型只能是标准席位、Usage-based 或 ProLite")
    invite_error = ""
    seats: dict[str, dict] = {}
    try:
        result = workspace_membership.invite_candidates(req.workspace_id, req.emails, seat_type=req.seat_type)
    except Exception as e:
        invite_error = str(e)
        logger.exception("候选管理母号邀请失败 workspace_db_id=%s count=%s，将继续校验邀请状态", req.workspace_id, len(req.emails))
    if not invite_error:
        # 邀请请求成功即代表上游已受理整批邀请：直接标记待接受邀请，
        # 跳过 invites/users 逐个复查（目标席位以本次请求为准回填）。
        states = {email.lower(): "pending_invite" for email in req.emails}
        for email in req.emails:
            db.update_workspace_candidate_status(req.workspace_id, email, "pending_invite")
            db.update_workspace_candidate_seat_type(req.workspace_id, email, req.seat_type)
        return {
            "ok": True,
            "result": result,
            "states": states,
            "seats": {},
            "invite_error": "",
            "recheck_error": "",
        }
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
        membership_result = workspace_membership.check_candidate_membership(
            req.workspace_id,
            req.emails,
            prefer_invites=True,
            include_seats=True,
        )
        if isinstance(membership_result, tuple):
            states, seats = membership_result
        else:
            # 兼容旧的替身/插件实现：即使只返回状态，也继续完成邀请状态写回。
            states = membership_result
            seats = {}
    except Exception as exc:
        recheck_error = str(exc)
        logger.exception("候选邀请后状态校验失败 workspace_db_id=%s", req.workspace_id)
        states = {email.lower(): "unknown" for email in req.emails}
    if not recheck_error:
        for email in req.emails:
            db.update_workspace_candidate_status(req.workspace_id, email, states.get(email.lower(), "not_invited"))
            seat_info = seats.get(email.lower()) or {}
            raw_seat_type = str(seat_info.get("raw_seat_type") or "").strip()
            # 邀请列表返回席位时优先使用上游真实值；部分上游版本不返回
            # seat_type，此时批量邀请请求中的目标席位是待邀请成员的可靠回退。
            if (
                not raw_seat_type
                and not invite_error
                and states.get(email.lower()) in {"pending_invite", "pending_request"}
            ):
                raw_seat_type = req.seat_type
            if raw_seat_type:
                db.update_workspace_candidate_seat_type(req.workspace_id, email, raw_seat_type)
    final_ok = not recheck_error and not any(value == "not_invited" for value in states.values())
    return {
        "ok": final_ok,
        "result": result if not invite_error else None,
        "states": states,
        "seats": seats,
        "invite_error": invite_error,
        "recheck_error": recheck_error,
    }


@app.post("/api/workspace-candidates/request-join")
def api_request_workspace_join(req: WorkspaceCandidatesReq):
    if not req.emails: raise HTTPException(400, "请选择候选人")
    _reject_permanently_invalid(req.emails)
    _reject_trashed_candidates(req.workspace_id, req.emails)
    if req.seat_type not in {"default", "usage_based", "prolite"}:
        raise HTTPException(400, "席位类型只能是标准席位、Usage-based 或 ProLite")
    rows = db.list_workspace_candidates(req.workspace_id)
    indexed = {r["email"]: r for r in rows}; results = []
    pool_values = _proxy_pool_values(
        _candidate_proxy_pool_text(
            db.get_workspace_settings(req.workspace_id), req.proxy_pool
        )
    )
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
        logger.error(
            "候选状态校验失败 workspace_db_id=%s emails=%s error_type=%s error=%s",
            req.workspace_id,
            emails,
            type(e).__name__,
            str(e)[:500],
        )
        logger.exception("候选状态校验失败 workspace_db_id=%s", req.workspace_id)
        status_code = 429 if getattr(e, "status_code", 0) == 429 else 400
        raise HTTPException(status_code, str(e))
    for email, status in states.items():
        db.update_workspace_candidate_status(req.workspace_id, email, status)
    for email, info in seats.items():
        db.update_workspace_candidate_seats(req.workspace_id, email, info.get("codex_seat", ""), info.get("gpt_seat", ""))
        db.update_workspace_candidate_member(req.workspace_id, email, info.get("member_id", ""), info.get("raw_seat_type", ""))
    return {"ok": True, "states": states, "seats": seats}


@app.post("/api/workspace-candidates/accept-invite")
def api_accept_workspace_invite(req: WorkspaceCandidatesReq):
    """用候选人个人凭证请求 accounts/check，把待接受邀请落地为已加入。

    成员侧只需一发 GET accounts/check 即可触发邀请接受（实测验证；
    wham/usage 与 auth/session 不触发），无需走 OAuth/浏览器登录。
    全部请求发完后统一用母号成员接口复核并落库 member_id/席位快照。
    """
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    indexed = _workspace_candidate_index(req.workspace_id)
    emails = list(dict.fromkeys(
        email.strip().lower()
        for email in req.emails
        if email.strip() and email.strip().lower() in indexed
    ))
    if not emails:
        raise HTTPException(400, "所选账号不是当前母号空间的候选人")
    settings = db.get_workspace_settings(req.workspace_id)
    try:
        leases = _candidate_quota_proxy_pool(
            _candidate_proxy_pool_text(settings, req.proxy_pool)
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    results = []
    responded_emails = []
    for email in emails:
        try:
            proxy = _lease_candidate_quota_proxy(
                leases,
                workspace_id=req.workspace_id,
                email=email,
                detail="accept-invite",
            )
            accepted = workspace_membership.accept_candidate_invite(
                req.workspace_id, email, proxy=proxy
            )
            responded_emails.append(email)
            results.append({
                "email": email,
                "ok": True,
                "accepted": bool(accepted),
                "error": "" if accepted else "请求成功但空间未出现（邀请可能已过期或被撤回）",
            })
        except Exception as exc:
            logger.warning(
                "接受邀请失败 workspace_db_id=%s email=%s error=%s",
                req.workspace_id, email, str(exc)[:300],
            )
            results.append({"email": email, "ok": False, "accepted": False, "error": str(exc)})

    # 母号复核：把真实成员状态/席位/member_id 落库。accounts/check 里出现
    # 空间已经能说明接受了邀请，但 member_id 和席位快照要靠母号侧补齐。
    states: dict[str, str] = {}
    if responded_emails:
        try:
            states, seats = workspace_membership.check_candidate_membership(
                req.workspace_id,
                responded_emails,
                include_seats=True,
            )
            for email, status in states.items():
                db.update_workspace_candidate_status(req.workspace_id, email, status)
            for email, info in seats.items():
                db.update_workspace_candidate_seats(
                    req.workspace_id, email, info.get("codex_seat", ""), info.get("gpt_seat", "")
                )
                db.update_workspace_candidate_member(
                    req.workspace_id, email, info.get("member_id", ""), info.get("raw_seat_type", "")
                )
        except Exception:
            logger.exception(
                "接受邀请后母号复核失败 workspace_db_id=%s", req.workspace_id
            )
    for item in results:
        item["status"] = states.get(item["email"], "")
    return {
        "ok": not any(not item.get("ok") for item in results),
        "results": results,
        "joined": sum(1 for s in states.values() if s == "joined"),
        "failed": sum(1 for item in results if not item.get("ok")),
    }


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
    if req.seat_type not in {"default", "usage_based", "prolite"}: raise HTTPException(400, "席位类型无效")
    # 席位切换是成员管理操作，允许处理已标记出库的成员；候选列表接口
    # 默认隐藏出库记录，因此这里显式合并 active/outbound 两个视图。
    indexed = {
        r["email"]: r
        for tag_filter in ("active", "outbound")
        for r in db.list_workspace_candidate_options(req.workspace_id, tag_status=tag_filter)
    }
    results = []
    def canonical_seat(value: object) -> str:
        # 历史数据可能保存过 usage-based / usagebased，统一后再比较。
        value = str(value or "").strip().lower().replace("-", "_")
        if value in {"usage_based", "usagebased", "codex席位"}:
            return "usage_based"
        if value in {"default", "standard", "standard_seat", "gpt席位", "标准席位"}:
            return "default"
        if value in {"prolite", "pro_lite", "advanced", "advanced_seat", "premium", "premium_seat", "pro", "高级", "高级席位"}:
            return "prolite"
        return value
    settings = db.get_workspace_settings(req.workspace_id)
    for email in req.emails:
        key = email.lower(); row = indexed.get(key)
        reserved = False
        prolite_reserved = False
        try:
            if not row: raise RuntimeError("候选人不属于当前空间")
            if row.get("workspace_join_status") != "joined":
                raise RuntimeError("候选人尚未加入当前空间")
            current_seat = canonical_seat(row.get("seat_label") or row.get("seat_type"))
            # 只有本地席位信息为空（不在已知范围内）才去远端获取
            if current_seat not in {"default", "usage_based", "prolite"}:
                fresh = workspace_membership.fetch_candidate_seats(req.workspace_id, [email]).get(key, {})
                if not row.get("member_id"):
                    row["member_id"] = fresh.get("member_id", "")
                current_seat = canonical_seat(
                    fresh.get("raw_seat_type")
                    or fresh.get("seat_type")
                    or fresh.get("gpt_seat")
                    or fresh.get("codex_seat")
                )
                # 同步更新本地席位信息
                if current_seat in {"default", "usage_based", "prolite"}:
                    db.update_workspace_candidate_member(req.workspace_id, email, fresh.get("member_id", row.get("member_id", "")), current_seat)
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
            if req.seat_type == "default" and current_seat in {"usage_based", "prolite"}:
                reservation = db.reserve_workspace_seat_protect_quota(req.workspace_id, 1)
                if not reservation.get("allowed", False):
                    raise RuntimeError(
                        "席位保护已生效：本周期标准席位切换已达 "
                        f"{int(reservation.get('threshold') or settings.get('seat_protect_threshold') or 8)} 个，"
                        f"下次刷新时间 {reservation.get('refresh_time') or settings.get('seat_protect_refresh_time') or '00:00'}"
                    )
                reserved = bool(reservation.get("enabled"))
            elif req.seat_type == "prolite" and current_seat != "prolite":
                reservation = db.reserve_workspace_prolite_seat_protect_quota(req.workspace_id, 1)
                if not reservation.get("allowed", False):
                    raise RuntimeError(
                        "高级席位保护已生效：本周期高级席位切换已达 "
                        f"{int(reservation.get('threshold') or settings.get('prolite_seat_protect_threshold') or 8)} 个，"
                        f"下次刷新时间 {reservation.get('refresh_time') or settings.get('prolite_seat_protect_refresh_time') or '00:00'}"
                    )
                prolite_reserved = bool(reservation.get("enabled"))
            result = workspace_membership.update_member_seat_type(req.workspace_id, row["member_id"], req.seat_type)
            db.update_workspace_candidate_member(req.workspace_id, email, row["member_id"], req.seat_type)
            if req.seat_type in {"default", "prolite"}:
                try:
                    db.increment_workspace_fulfillment_counter(req.workspace_id, req.seat_type, 1)
                except Exception:
                    logger.exception("席位切换历史计数递增失败 workspace_db_id=%s email=%s seat_type=%s", req.workspace_id, key, req.seat_type)
            results.append({"email": key, "ok": True, "skipped": False, "result": result})
        except Exception as e:
            logger.error(
                "候选席位切换失败 workspace_db_id=%s email=%s target_seat=%s error_type=%s error=%s",
                req.workspace_id,
                key,
                req.seat_type,
                type(e).__name__,
                str(e)[:500],
            )
            if reserved:
                try:
                    db.release_workspace_seat_protect_quota(req.workspace_id, 1)
                except Exception:
                    logger.exception("席位保护配额回滚失败 workspace_db_id=%s email=%s", req.workspace_id, key)
            if prolite_reserved:
                try:
                    db.release_workspace_prolite_seat_protect_quota(req.workspace_id, 1)
                except Exception:
                    logger.exception("高级席位保护配额回滚失败 workspace_db_id=%s email=%s", req.workspace_id, key)
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
    # 手动查询不带垃圾箱设置，模型默认值不能盖掉空间已保存的口径（否则关掉
    # 自动入箱、或选了 5h/周窗口的空间，一手动查询就被打回默认行为）。
    for key in ("trash_enabled", "trash_invalid_enabled", "trash_zero_delay_minutes",
                "trash_zero_quota_window", "trash_gap_seconds"):
        if key not in req.model_fields_set:
            setting_overrides.pop(key, None)
    settings = _workspace_settings_snapshot(req.workspace_id, setting_overrides)
    try:
        quota_leases = _candidate_quota_proxy_pool(
            _candidate_proxy_pool_text(settings, req.proxy_pool),
            preferred_proxy=req.quota_proxy,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    candidate_rows = {
        str(row.get("email") or "").strip().lower(): row
        for row in db.list_workspace_candidate_options(req.workspace_id)
        if str(row.get("email") or "").strip()
    }
    results = {}
    logging.getLogger("workspace_membership").info("额度查询开始 workspace=%s count=%s relogin_on_401=%s auto_push=%s proxy_configured=%s", req.workspace_id, len(req.emails), req.relogin_on_401, req.auto_push, bool(_candidate_proxy_pool_text(settings, req.proxy_pool)))
    trash_delay = _candidate_trash_delay_seconds(req.workspace_id, settings)
    trash_window = _candidate_trash_zero_quota_window(req.workspace_id, settings)
    for email in req.emails:
        key = email.strip().lower()
        ineligible_reason = _candidate_quota_ineligible_reason(
            candidate_rows.get(key), strict_seat=False
        )
        if ineligible_reason:
            results[key] = {"ok": False, "error": ineligible_reason, "skipped": True}
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
                network_retries=_candidate_quota_network_retries(req.workspace_id, settings),
            )
            results[key] = {"ok": True, "quota": quota}
        except workspace_membership.QuotaAccountDeactivated as e:
            _handle_candidate_quota_deactivated(
                req.workspace_id, key, settings, e, source="quota_manual"
            )
            results[key] = {"ok": False, "error": str(e), "status": "deactivated"}
            if _candidate_trash_invalid_enabled(req.workspace_id, settings):
                results[key]["trashed"] = True
            continue
        except workspace_membership.QuotaPaymentRequired as e:
            results[key] = {"ok": False, "error": str(e), "status": "payment_required"}
            continue
        except Exception as e:
            results[key] = {"ok": False, "error": str(e)}
            is_401 = isinstance(e, workspace_membership.QuotaUnauthorized)
            if req.relogin_on_401 and is_401:
                if not _candidate_proxy_pool_text(settings):
                    results[key]["relogin_error"] = "候选人代理池为空，无法重新登录"
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
                        network_retries=_candidate_quota_network_retries(req.workspace_id, settings),
                    )
                    results[key] = {"ok": True, "quota": quota, "relogin_started": True}
                except Exception as relogin_exc:
                    results[key]["relogin_error"] = str(relogin_exc)
                    continue
            else:
                continue
        if quota is not None and _is_zero_quota_payload(quota, trash_window) and _candidate_trash_enabled(req.workspace_id, settings):
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
    # Older clients do not send the seat-automation fields.  Merge only
    # explicitly supplied request values so starting quota polling cannot
    # silently disable an already-running seat replenishment task.
    settings = db.get_workspace_settings(req.workspace_id)
    for key in req.model_fields_set:
        if key != "workspace_id":
            value = getattr(req, key)
            if key == "auto_prolite_candidate_seat_type":
                value = _normalize_auto_prolite_candidate_seat_type(value)
            elif key == "trash_zero_quota_window":
                value = _normalize_trash_zero_quota_window(value)
            settings[key] = value
    settings["quota_enabled"] = True
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
    next_at = time.time() + _auto_seat_interval_seconds(settings)
    with _seat_auto_schedulers_lock:
        _seat_auto_schedulers[req.workspace_id] = (stop, thread, next_at)
    thread.start()
    return {
        "ok": True,
        "running": True,
        "interval_minutes": _auto_seat_interval_seconds(settings) // 60,
        "settings": {**settings, "auto_standard_seat_enabled": True},
        "next_at": next_at,
    }


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
            next_at = time.time() + _auto_seat_interval_seconds(cfg)
            item = (stop, thread, next_at)
            _seat_auto_schedulers[workspace_id] = item
            thread.start()
    return {
        "ok": True,
        "running": bool(item and item[1].is_alive()),
        "interval_minutes": _auto_seat_interval_seconds(cfg) // 60,
        "next_at": item[2] if item else 0,
        "settings": cfg,
    }


@app.post("/api/workspace-candidates/auto-prolite-seat/start")
def api_start_auto_prolite_seat(req: WorkspaceAutoSeatReq):
    settings = db.get_workspace_settings(req.workspace_id)
    with _prolite_auto_schedulers_lock:
        old = _prolite_auto_schedulers.pop(req.workspace_id, None)
    if old:
        old[0].set()
    db.update_workspace_settings(req.workspace_id, {"auto_prolite_seat_enabled": True})
    stop = threading.Event()
    thread = threading.Thread(
        target=_auto_prolite_seat_worker,
        args=(req.workspace_id, stop),
        daemon=True,
        name=f"seat-auto-prolite-{req.workspace_id}",
    )
    next_at = time.time() + _auto_seat_interval_seconds(settings)
    with _prolite_auto_schedulers_lock:
        _prolite_auto_schedulers[req.workspace_id] = (stop, thread, next_at)
    thread.start()
    return {
        "ok": True,
        "running": True,
        "interval_minutes": _auto_seat_interval_seconds(settings) // 60,
        "settings": {**settings, "auto_prolite_seat_enabled": True},
        "next_at": next_at,
    }


@app.post("/api/workspace-candidates/auto-prolite-seat/stop")
def api_stop_auto_prolite_seat(req: WorkspaceAutoSeatReq):
    with _prolite_auto_schedulers_lock:
        item = _prolite_auto_schedulers.pop(req.workspace_id, None)
    if item:
        item[0].set()
    db.update_workspace_settings(req.workspace_id, {"auto_prolite_seat_enabled": False})
    return {"ok": True, "running": False}


@app.get("/api/workspace-candidates/auto-prolite-seat")
def api_auto_prolite_seat_status(workspace_id: int):
    cfg = db.get_workspace_settings(workspace_id)
    with _prolite_auto_schedulers_lock:
        item = _prolite_auto_schedulers.get(workspace_id)
        if item and not item[1].is_alive():
            _prolite_auto_schedulers.pop(workspace_id, None)
            item = None
        if not item and cfg.get("auto_prolite_seat_enabled"):
            stop = threading.Event()
            thread = threading.Thread(
                target=_auto_prolite_seat_worker,
                args=(workspace_id, stop),
                daemon=True,
                name=f"seat-auto-prolite-{workspace_id}",
            )
            next_at = time.time() + _auto_seat_interval_seconds(cfg)
            item = (stop, thread, next_at)
            _prolite_auto_schedulers[workspace_id] = item
            thread.start()
    return {
        "ok": True,
        "running": bool(item and item[1].is_alive()),
        "interval_minutes": _auto_seat_interval_seconds(cfg) // 60,
        "next_at": item[2] if item else 0,
        "settings": cfg,
    }


# Compatibility aliases for clients that call this feature “advanced seat”.
@app.post("/api/workspace-candidates/auto-advanced-seat/start")
def api_start_auto_advanced_seat(req: WorkspaceAutoSeatReq):
    return api_start_auto_prolite_seat(req)


@app.post("/api/workspace-candidates/auto-advanced-seat/stop")
def api_stop_auto_advanced_seat(req: WorkspaceAutoSeatReq):
    return api_stop_auto_prolite_seat(req)


@app.get("/api/workspace-candidates/auto-advanced-seat")
def api_auto_advanced_seat_status(workspace_id: int):
    return api_auto_prolite_seat_status(workspace_id)


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
    settings = db.get_workspace_settings(req.workspace_id)
    for key in req.model_fields_set:
        if key != "workspace_id":
            value = getattr(req, key)
            if key == "auto_prolite_candidate_seat_type":
                value = _normalize_auto_prolite_candidate_seat_type(value)
            elif key == "trash_zero_quota_window":
                value = _normalize_trash_zero_quota_window(value)
            settings[key] = value
    db.update_workspace_settings(req.workspace_id, settings)
    return {"ok": True}


class WorkspacePushTestReq(BaseModel):
    workspace_id: int
    target: str = Field(..., description="推送目标：cpa / sub2api")


@app.post("/api/workspace-candidates/push-test")
def api_workspace_push_test(req: WorkspacePushTestReq):
    """按「空间专属覆盖 + 全局兜底」后的生效配置，测试推送目标连通性。"""
    from . import exporter

    target = str(req.target or "").strip().lower()
    cfg = db.get_export_internal_config()
    settings = db.get_workspace_settings(req.workspace_id)

    def _merged(base_key: str, pairs: tuple[tuple[str, str], ...]) -> dict:
        merged = dict(cfg.get(base_key) or {})
        for setting_key, cfg_key in pairs:
            value = str(settings.get(setting_key) or "").strip()
            if value:
                merged[cfg_key] = value
        return merged

    try:
        if target == "sub2api":
            return {"ok": True, "result": exporter.test_sub2api(_merged("sub2api", (
                ("auto_push_sub2api_url", "sub2api_url"),
                ("auto_push_sub2api_api_key", "sub2api_api_key"),
                ("auto_push_sub2api_group_ids", "sub2api_group_ids"),
            )))}
        if target == "cpa":
            return {"ok": True, "result": exporter.test_cpa(_merged("cpa", (
                ("auto_push_cpa_url", "cpa_url"),
                ("auto_push_cpa_mgmt_key", "cpa_mgmt_key"),
            )))}
    except Exception as e:
        raise HTTPException(400, str(e))
    raise HTTPException(400, f"未知推送目标: {req.target}")


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
    proxy_pool = _candidate_proxy_pool_text(
        db.get_workspace_settings(req.workspace_id), req.proxy_pool
    )
    if not proxy_pool:
        raise HTTPException(400, "候选人代理池为空")
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
        "target_count": 0,
    })
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "空间凭证任务启动失败"))
    return {"ok": True, "run": result, "workspace_id": master["workspace_id"], "eligible": len(eligible), "skipped": len(skipped), "skipped_emails": skipped}


@app.post("/api/workspace-candidates/login-only")
def api_workspace_login_only(req: WorkspaceCandidatesReq):
    """仅登录空间（跳过 OAuth，不获取 access_token）"""
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
            logger.exception("仅登录任务候选状态校验失败 workspace_db_id=%s", req.workspace_id)
            status_code = 429 if getattr(exc, "status_code", 0) == 429 else 400
            raise HTTPException(status_code, str(exc))
        for email, status in states.items():
            db.update_workspace_candidate_status(req.workspace_id, email, status)
            if status == "pending_request":
                workspace_membership.approve_candidate_request(req.workspace_id, email, req.seat_type)
                db.update_workspace_candidate_status(req.workspace_id, email, "approved")
                skipped.append(email)
            elif status in {"joined", "pending_invite"}:
                eligible.append(email)
            else:
                skipped.append(email)
    eligible = trusted_eligible + eligible
    proxy_pool = _candidate_proxy_pool_text(
        db.get_workspace_settings(req.workspace_id), req.proxy_pool
    )
    if not proxy_pool:
        raise HTTPException(400, "候选人代理池为空")
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
        "proxy": "", "proxy_usage_detail": "workspace_login_only",
        "concurrency": req.concurrency, "otp_timeout": req.otp_timeout,
        "want_access_token": False, "want_session_token": True, "want_refresh_token": False,
        "want_password": False, "want_2fa": False, "allow_existing_login": True,
        "cool_down_seconds": req.cool_down_seconds, "account_retry_count": req.account_retry_count, "auto_export": False,
        "target_count": 0,
    })
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "仅登录任务启动失败"))
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


class AccountPasswordReq(BaseModel):
    email: str = Field(..., min_length=1, description="要录入密码的邮箱")
    password: str = Field(..., min_length=1, description="OpenAI 登录密码")


@app.post("/api/accounts/update_password")
def api_update_account_password(req: AccountPasswordReq):
    """从邮箱列表手动录入 OpenAI 登录密码。

    已有注册结果的账号更新 registered；尚未注册的账号更新号池行，后续
    仅登录/补齐 2FA 快照会读取该密码。只保存本地记录，不会修改远端密码。
    """
    email = str(req.email or "").strip().lower()
    password = str(req.password or "").strip()
    if not email:
        raise HTTPException(400, "email 不能为空")
    if not password:
        raise HTTPException(400, "密码不能为空")
    target = db.update_account_password(email, password)
    if not target:
        raise HTTPException(404, f"未找到邮箱: {email}")
    logger.info("[accounts] 手动录入密码 email=%s target=%s", email, target)
    return {"ok": True, "email": email, "target": target}


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

    # 单次注册也必须接入系统代理池。前端 proxy 为空时，从本次任务快照中
    # 领取租用次数最少的代理；显式填写 proxy 则保留用户指定值。
    effective_proxy = str(req.proxy or "").strip()
    if not effective_proxy and str(req.proxy_pool or "").strip():
        pool_values = [line.strip() for line in str(req.proxy_pool).splitlines() if line.strip()]
        pool = public_relogin.ProxyLeasePool(pool_values)
        effective_proxy, pool_index, leased_count = pool.lease(
            task_type="register",
            task_detail=f"single_{req.register_mode or 'protocol'}",
        )
        logger.info(
            "单次注册领取系统代理 register_mode=%s pool_index=%s leased_count=%s",
            req.register_mode or "protocol", pool_index + 1, leased_count,
        )

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "want_password": req.want_password,
        "proxy": effective_proxy,
        "otp_timeout": int(req.otp_timeout),
        "add_phone_mode": req.add_phone_mode,
        "register_mode": req.register_mode if req.register_mode in {"protocol", "camoufox"} else "protocol",
        "debug_mode": bool(req.debug_mode),
        "allow_existing_login": req.allow_existing_login,
        "want_2fa": req.want_2fa,
        # 普通注册若服务端把邮箱识别为已有账号，只按 2FA 开关进入补齐2FA；
        # want_password 仅控制新账号注册密码，不会触发已有账号密码创建。
        "ensure_credentials": (
            bool(req.ensure_credentials)
            if req.ensure_credentials is not None
            else bool(req.want_2fa)
        ),
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


def _workspace_export_key(workspace_db_id: int, rows: list[dict] | None = None) -> str:
    """Return the key used for a Team/workspace Sub2 export.

    The external ChatGPT workspace id shown in workspace management is the
    normal key.  Token/row fields cover legacy records and test doubles.  The
    local workspace record id is only a deterministic final fallback; it is
    also written into the exported ``credentials.workspace_id`` field, so the
    recipient can still decrypt instead of receiving accidental plaintext.
    """
    try:
        master = db.get_workspace_master(int(workspace_db_id or 0)) or {}
    except Exception:
        master = {}
    for key in ("workspace_id", "workspaceId"):
        value = str(master.get(key) or "").strip()
        if value:
            return value
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for key in ("workspace_id", "workspaceId", "chatgpt_account_id", "chatgptAccountId"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
        legacy_master_id = row.get("workspace_master_id")
        try:
            legacy_master_id = int(legacy_master_id) if legacy_master_id else 0
        except (TypeError, ValueError):
            legacy_master_id = 0
        if legacy_master_id and legacy_master_id != int(workspace_db_id or 0):
            try:
                legacy_master = db.get_workspace_master(legacy_master_id) or {}
            except Exception:
                legacy_master = {}
            value = str(legacy_master.get("workspace_id") or "").strip()
            if value:
                return value
        try:
            payload = _decode_jwt_payload(str(row.get("access_token") or ""))
            auth = _get_auth(payload)
            value = str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
            if value:
                return value
        except Exception:
            pass
    # Some legacy workspace-credential rows only persisted account_id.  Use
    # it as a last compatibility fallback after inspecting the token claim.
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        value = str(row.get("account_id") or row.get("accountId") or "").strip()
        if value:
            return value
    fallback = int(workspace_db_id or 0)
    if fallback:
        return str(fallback)
    raise ValueError("母号缺少 Workspace ID，无法加密 Sub2 凭证")


class ExportRegisteredReq(BaseModel):
    format: str = Field(..., description="格式 id，见 GET /api/registered/export/formats")
    emails: Optional[list[str]] = Field(None, description="要导出的 email 列表")
    all: bool = Field(False, description="true = 导出全部（跨页），忽略 emails")
    workspace_id: Optional[int] = Field(None, description="按指定 Team 空间导出其独立凭证")
    proxy_pool: str = Field("", description="刷新 Sub2 OAuth token 使用的代理池")
    refresh_oauth: Optional[bool] = Field(
        None,
        description="Sub2 导出前是否用 refresh_token 刷新；留空跟随全局导出设置",
    )
    encrypt_credentials: Optional[bool] = Field(
        None,
        description="CPA/Sub2 导出时是否用 Workspace ID 加密 password/totp_secret",
    )
    mark_outbound: bool = Field(
        False,
        description="导出成功后将候选人标记为 outbound（仅 Workspace Sub2 导出）",
    )
    cpa_template: Optional[bool] = Field(
        None,
        description="CPA 导出时是否按「导出模版配置」写入凭证级代理 proxy_url 和启停 disabled",
    )


@app.post("/api/registered/export")
def api_export_registered(req: ExportRegisteredReq):
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")

    # 候选管理页带 workspace_id，但邮箱/密码/2FA 是注册结果的账号凭证，
    # 不属于 workspace_credentials。若继续统一走空间凭证查询，会在部分
    # 候选尚未同步空间凭证时丢掉密码/TOTP，导出成空字段。文本凭证格式
    # 始终按勾选邮箱读取 registered（同时 LEFT JOIN 带出 relay_url）。
    credential_text_formats = {"email_pw", "email_pw_2fa", "email_pw_2fa_relay"}
    if req.workspace_id and req.format in credential_text_formats:
        rows = db.list_registered_by_emails(req.emails or [])
    elif req.workspace_id:
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
    # CPA 不做 refresh_token 刷新。Sub2 是否刷新由请求级开关覆盖全局设置；
    # 关闭时仅校验现有 AT/ID 是否同源，不访问 /oauth/token。
    if fmt.id == "sub2api":
        from . import exporter as _exp
        export_cfg = db.get_export_internal_config().get("sub2api", {})
        refresh_oauth = (
            bool(req.refresh_oauth)
            if req.refresh_oauth is not None
            else bool(export_cfg.get("refresh_oauth"))
        )
        refresh_leases = None
        if refresh_oauth:
            refresh_proxies = _public_relogin_proxy_pool(req.proxy_pool, "")
            refresh_leases = public_relogin.ProxyLeasePool(refresh_proxies)
        refreshed_rows = []
        for r in rows:
            rt = str(r.get("refresh_token") or "").strip()
            if refresh_oauth and rt:
                try:
                    refresh_proxy, _, _ = refresh_leases.lease(
                        task_type="other",
                        task_detail="sub2_token_refresh",
                    )
                    fresh = _exp.refresh_codex_token(
                        rt,
                        timeout=30,
                        proxy=refresh_proxy,
                        client_id=_exp.CODEX_CLIENT_ID,
                    )
                    _exp.validate_sub2_token_pair(
                        fresh.get("access_token", ""),
                        fresh.get("id_token", ""),
                    )
                    r = {
                        **r,
                        "access_token":  fresh["access_token"],
                        "refresh_token": fresh.get("refresh_token") or r.get("refresh_token"),
                        "id_token":      fresh.get("id_token") or r.get("id_token", ""),
                    }
                    _exp.validate_sub2_token_pair(r.get("access_token", ""), r.get("id_token", ""))
                    # 刷新 token 会轮换 RT，必须把整组 AT/RT/ID 写回正确的
                    # 凭证域。候选导出写 workspace_credentials，不能污染
                    # registered 中的 Personal 凭证。
                    try:
                        if req.workspace_id:
                            db.save_workspace_credential(req.workspace_id, r)
                        else:
                            db.update_registered_oauth_tokens(
                                r.get("email", ""),
                                access_token=r.get("access_token", ""),
                                refresh_token=r.get("refresh_token", ""),
                                id_token=r.get("id_token", ""),
                            )
                    except Exception:
                        logger.exception(
                            "Sub2 刷新凭证回写失败 email=%s workspace_db_id=%s",
                            r.get("email", ""), req.workspace_id,
                        )
                        raise
                except Exception as e:
                    raise HTTPException(
                        502,
                        f"Sub2 导出前 refresh_token 刷新失败（{r.get('email', '')}）：{e}",
                    ) from e
            else:
                try:
                    _exp.validate_sub2_token_pair(
                        r.get("access_token", ""),
                        r.get("id_token", ""),
                    )
                except Exception as e:
                    raise HTTPException(
                        502,
                        f"Sub2 导出前凭证一致性校验失败（{r.get('email', '')}）：{e}",
                    ) from e
            refreshed_rows.append(r)
        rows = refreshed_rows

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
        # CPA「按模版导出」：勾选时把模版里的凭证级代理 proxy_url 和启停
        # disabled 写进每个凭证 JSON；不勾选则与旧导出完全一致。
        cpa_template = (
            db.get_cpa_export_template()
            if fmt.id == "cpa" and req.cpa_template
            else None
        )
        try:
            if fmt.id in {"cpa", "sub2api", "sub2api_lines"} and req.workspace_id:
                # CPA and Sub2 use the exact same switch and Workspace key;
                # this prevents one format from accidentally leaking a
                # password/TOTP pair while the other protects it.
                encrypt_credentials = (
                    bool(req.encrypt_credentials)
                    if req.encrypt_credentials is not None
                    else True
                )
                blob = export_formats.render_bytes(
                    rows,
                    fmt,
                    workspace_id=_workspace_export_key(req.workspace_id, rows),
                    encrypt_credentials=encrypt_credentials,
                    cpa_template=cpa_template,
                )
            else:
                if req.encrypt_credentials:
                    raise ValueError("加密 CPA/Sub2 凭证必须指定 Workspace")
                # Pass an explicit false for the two credential formats so
                # the renderer applies the same plaintext decision to CPA and
                # Sub2 even when no Workspace is selected.
                blob = export_formats.render_bytes(
                    rows,
                    fmt,
                    encrypt_credentials=False if fmt.id in {"cpa", "sub2api", "sub2api_lines"} else None,
                    cpa_template=cpa_template,
                )
        except Exception as exc:
            prefix = "Sub2 导出凭证一致性校验失败" if fmt.id == "sub2api" else "导出文件生成失败"
            raise HTTPException(502, f"{prefix}：{exc}") from exc
        if req.mark_outbound:
            if fmt.id != "sub2api" or not req.workspace_id or req.encrypt_credentials is not True:
                raise HTTPException(400, "出库并导出只能使用当前 Workspace 的加密 Sub2 格式")
            marked = db.update_workspace_candidate_tag_status(
                req.workspace_id,
                [str(email).strip().lower() for email in req.emails or [] if str(email).strip()],
                "outbound",
            )
            base["outbound_marked"] = marked
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob)}

    return {**base, "text": export_formats.render_text(rows, fmt)}


@app.post("/api/workspace-candidates/export-outbound")
def api_workspace_candidates_export_outbound(req: WorkspaceExportOutboundReq):
    """加密导出当前 Workspace 候选人，并在文件生成成功后标记出库。"""
    if not req.emails:
        raise HTTPException(400, "请选择候选人")
    emails = list(dict.fromkeys(str(email).strip().lower() for email in req.emails if str(email).strip()))
    if not emails:
        raise HTTPException(400, "没有有效的候选人邮箱")
    options = {
        str(row.get("email") or "").strip().lower(): row
        for row in db.list_workspace_candidate_options(req.workspace_id, tag_status="active")
    }
    missing = [email for email in emails if email not in options]
    if missing:
        raise HTTPException(400, "以下账号不是当前 Workspace 的正常候选人：" + ", ".join(missing))
    missing_credentials = [
        email for email in emails if not bool(options[email].get("has_workspace_access_token"))
    ]
    if missing_credentials:
        raise HTTPException(400, "以下账号尚未获得当前 Workspace 凭证，不能导出并出库：" + ", ".join(missing_credentials))
    blocked = [
        email for email in emails
        if options[email].get("trash_status") == "trashed"
        or options[email].get("account_status") == "permanently_invalid"
    ]
    if blocked:
        raise HTTPException(409, "垃圾箱或永久失效账号不能出库：" + ", ".join(blocked))
    return api_export_registered(
        ExportRegisteredReq(
            format="sub2api",
            emails=emails,
            workspace_id=req.workspace_id,
            proxy_pool=req.proxy_pool,
            refresh_oauth=req.refresh_oauth,
            encrypt_credentials=True,
            mark_outbound=True,
        )
    )


# ──────────────────────── 兑换码 ────────────────────────
#
# 兑换码把「一个空间凭证」变成「一串可分发出去的 12 位码」。生成幂等：
# 同一 (空间, 账号) 永远对应同一个码，重复导出只是再拿一次。持码人走
# 公开的 /api/redeem 反复下载绑定账号的加密 Sub2/CPA 凭证，凭证内容跟随
# workspace_credentials 里的最新值。作废即删行，下次导出会生成新码。

REDEEM_FORMATS = {"sub2api", "cpa", "email_pw_2fa"}


class RedeemCodesGenerateReq(BaseModel):
    workspace_id: int = Field(..., description="母号记录 ID")
    emails: list[str] = Field(..., min_length=1, description="候选账号邮箱")
    allow_secret: bool = Field(
        False, description="允许持码人兑换明文 账号----密码----2FA（默认关闭）"
    )


@app.post("/api/workspace-candidates/redeem-codes")
def api_workspace_candidates_redeem_codes(req: RedeemCodesGenerateReq):
    """为已持有当前 Workspace 空间凭证的候选人生成/取出兑换码，按行导出。"""
    emails = list(dict.fromkeys(str(e).strip().lower() for e in req.emails if str(e).strip()))
    if not emails:
        raise HTTPException(400, "请选择候选人")
    cred_rows = db.list_workspace_credentials_by_emails(req.workspace_id, emails)
    credentialed = {
        str(r.get("email") or "").strip().lower()
        for r in cred_rows
        if str(r.get("access_token") or "").strip()
    }
    eligible = [e for e in emails if e in credentialed]
    skipped = [e for e in emails if e not in credentialed]
    if not eligible:
        raise HTTPException(400, "所选账号都没有当前 Workspace 的空间凭证，无法生成兑换码")
    code_map = db.get_or_create_redeem_codes(
        req.workspace_id, eligible, allow_secret=req.allow_secret
    )
    lines = [code_map[e] for e in eligible if e in code_map]
    return {
        "ok": True,
        "count": len(lines),
        "label": "兑换码",
        "filename": f"redeem-codes-{len(lines)}.txt",
        "text": "\n".join(lines),
        "emails": [e for e in eligible if e in code_map],
        "skipped": skipped,
    }


class RedeemReq(BaseModel):
    code: str = Field("", description="单个 12 位兑换码")
    codes: Optional[list[str]] = Field(
        None, description="批量兑换码（每行一个）；提供时忽略 code"
    )
    format: str = Field(
        "sub2api", description="导出格式：sub2api / cpa / email_pw_2fa"
    )


REDEEM_BATCH_LIMIT = 500


@app.post("/api/redeem")
def api_redeem(req: RedeemReq):
    """公开兑换：凭码导出绑定账号的空间凭证（始终用各自的 Workspace ID 加密）。

    可重复兑换，但每个码永远只出它绑定的那个账号。codes 批量兑换时逐码
    校验：无效/失效码计入 failed，其余照常合并导出。管理端路径在
    /api/redeem-codes，公开白名单里对本路径是精确匹配，别改成前缀匹配。
    """
    fmt = export_formats.get_format(req.format)
    if fmt is None or fmt.id not in REDEEM_FORMATS:
        raise HTTPException(400, "兑换只支持 sub2api / cpa / email_pw_2fa 格式")
    # 明文 账号----密码----2FA 是码级权限（redeem_codes.allow_secret），
    # 与 Sub2/CPA 的 Workspace ID 加密走两条完全不同的渲染路径。
    is_secret_text = fmt.id == "email_pw_2fa"

    raw = list(req.codes) if req.codes else [req.code]
    codes = list(
        dict.fromkeys(
            key for key in (db.normalize_redeem_code(c) for c in raw) if key
        )
    )
    if not codes:
        raise HTTPException(400, "请输入兑换码")
    if len(codes) > REDEEM_BATCH_LIMIT:
        raise HTTPException(400, f"一次最多兑换 {REDEEM_BATCH_LIMIT} 个码")

    rows: list[dict] = []
    keys: list[str] = []
    emails: list[str] = []
    used_codes: list[str] = []
    failed: list[dict] = []
    for code in codes:
        entry = db.get_redeem_code(code)
        if not entry:
            failed.append({"code": code, "error": "兑换码不存在或已作废", "status": 404})
            continue
        cred = [
            r
            for r in db.list_workspace_credentials_by_emails(
                entry["workspace_master_id"], [entry["email"]]
            )
            if str(r.get("access_token") or "").strip()
        ]
        if not cred:
            failed.append(
                {"code": code, "error": "绑定账号的空间凭证已不存在", "status": 410}
            )
            continue
        if is_secret_text:
            if not entry.get("allow_secret"):
                failed.append(
                    {
                        "code": code,
                        "error": "该兑换码未开启「账号密码+2FA」兑换",
                        "status": 403,
                    }
                )
                continue
        else:
            try:
                key = _workspace_export_key(entry["workspace_master_id"], cred)
            except Exception as exc:
                failed.append({"code": code, "error": f"加密密钥缺失：{exc}", "status": 502})
                continue
            keys.append(key)
        rows.append(cred[0])
        emails.append(entry["email"])
        used_codes.append(code)

    if not rows:
        first = failed[0] if failed else {"error": "没有可兑换的有效兑换码", "status": 404}
        if len(failed) == 1:
            raise HTTPException(first["status"], first["error"])
        raise HTTPException(
            first["status"],
            "；".join(f"{f['code']}: {f['error']}" for f in failed[:5]),
        )

    common = {
        "ok": True,
        "email": emails[0] if len(emails) == 1 else "",
        "emails": emails,
        "count": len(rows),
        "failed": [{"code": f["code"], "error": f["error"]} for f in failed],
        "label": fmt.label,
    }
    if is_secret_text:
        text = export_formats.render_text(rows, fmt)
        for code in used_codes:
            db.mark_redeem_code_used(code)
        return {
            **common,
            "text": text,
            "filename": f"账号密码2FA-{len(rows)}.txt",
            "mime": "text/plain; charset=utf-8",
        }

    try:
        blob = export_formats.render_bytes_keyed(rows, keys, fmt)
    except Exception as exc:
        raise HTTPException(502, f"凭证文件生成失败：{exc}") from exc
    for code in used_codes:
        db.mark_redeem_code_used(code)
    return {
        **common,
        "filename": fmt.filename_for(rows) if fmt.filename_for else fmt.filename,
        "mime": fmt.mime_for(rows) if fmt.mime_for else fmt.mime,
        "b64": base64.b64encode(blob).decode("ascii"),
        "size": len(blob),
    }


@app.get("/api/redeem-codes")
def api_list_redeem_codes(workspace_id: int = 0):
    """兑换管理页：全部兑换码 + 所属空间 + 绑定账号 + 兑换统计。"""
    return {"ok": True, "codes": db.list_redeem_codes(workspace_id)}


class RedeemCodeDeleteReq(BaseModel):
    codes: list[str] = Field(..., min_length=1, description="要作废的兑换码")


@app.post("/api/redeem-codes/delete")
def api_delete_redeem_codes(req: RedeemCodeDeleteReq):
    deleted = db.delete_redeem_codes(req.codes)
    return {"ok": True, "deleted": deleted, "codes": db.list_redeem_codes()}


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
    sub2api_refresh_oauth: Optional[str] = None  # "0" / "1"


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


class CpaExportTemplateReq(BaseModel):
    proxy_url: Optional[str] = Field(None, description="写进凭证 JSON 的 proxy_url")
    file_enabled: Optional[bool] = Field(None, description="启用凭证文件 → disabled 取反")


@app.get("/api/settings/cpa-export-template")
def api_get_cpa_export_template():
    return {"ok": True, "template": db.get_cpa_export_template()}


@app.post("/api/settings/cpa-export-template")
def api_save_cpa_export_template(req: CpaExportTemplateReq):
    db.save_cpa_export_template(req.model_dump(exclude_none=True))
    return {"ok": True, "template": db.get_cpa_export_template()}


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
    refresh_oauth: Optional[bool] = Field(
        None,
        description="Sub2 导出前是否用 refresh_token 刷新；留空跟随全局导出设置",
    )


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
        if req.refresh_oauth is not None:
            sub2api_cfg["refresh_oauth"] = bool(req.refresh_oauth)
        try:
            out["sub2api"] = exporter.export_to_sub2api(
                cred, sub2api_cfg,
                on_tokens_refreshed=lambda fresh_cred: db.update_registered_oauth_tokens(
                    fresh_cred.get("email", ""),
                    access_token=fresh_cred.get("access_token", ""),
                    refresh_token=fresh_cred.get("refresh_token", ""),
                    id_token=fresh_cred.get("id_token", ""),
                ),
            )
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
    want_password: bool = True  # 新账号是否强制创建密码（不影响已有账号补齐2FA）
    login_only: bool = False    # 仅投送当前分组已有注册结果，刷新登录凭证
    # 仅登录时补齐缺失 2FA；普通注册遇到已有邮箱时也复用该开关。
    ensure_credentials: bool = True
    login_no_rt_only: bool = False  # 仅对无 RT 的注册结果执行
    login_emails: Optional[list[str]] = None  # 仅登录时限定指定账号（注册结果页重登录）
    workspace_id: str = ""  # 空间凭证获取时强制选择目标 Workspace
    group_name: str = Field("", description="邮箱分组；空=未分组，__all__=全部")
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    proxy_pool: str = ""         # 多代理池（每行一个）；优先于 proxy
    concurrency: int = 1         # 并发 worker 数（1-20）
    otp_timeout: int = 10
    add_phone_mode: str = Field("api", description="add-phone 验证模式：api / camoufox")
    register_mode: str = Field("protocol", description="注册流程：protocol / camoufox")
    debug_mode: bool = Field(False, description="Camoufox 调试模式：失败时保存页面截图")
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
def api_auto_pause(task_id: str = ""):
    res = (task_controller_for(task_id) or _active_auto_controller()).pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "暂停失败"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume(task_id: str = ""):
    res = (task_controller_for(task_id) or _active_auto_controller()).resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "恢复失败"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop(task_id: str = ""):
    res = (task_controller_for(task_id) or _active_auto_controller()).stop()
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
        "tasks": [c.status() for c in all_task_controllers()],
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run("webui.app:app", host="127.0.0.1", port=args.port, reload=False)
