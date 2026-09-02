"""公开 401 重登录页的无落库业务逻辑。"""
from __future__ import annotations

import logging
import json
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from typing import Callable
from typing import Any

from config import Config
from auth_flow import AuthFlow
from http_client import create_http_session
from mail_providers.base import MailProvider

from . import db, exporter, proxy_usage

BASE = "https://chatgpt.com"
logger = logging.getLogger("public_relogin")


class PublicQuotaUnauthorized(RuntimeError):
    pass


class PublicAccountDeactivated(RuntimeError):
    pass


class PublicTaskQueueFull(RuntimeError):
    pass


class BoundedTaskDispatcher:
    """进程级有界任务队列，所有公开页访问者共享并发额度。"""

    def __init__(self, name: str, default_capacity: int):
        self.name = name
        self._capacity = max(1, int(default_capacity))
        self._concurrency = 1
        self._queue: deque[tuple[Future, Callable[[], Any]]] = deque()
        self._condition = threading.Condition()
        self._workers: list[threading.Thread] = []
        self._running = 0
        self._submitted = 0
        self._completed = 0
        self._rejected = 0

    def configure(self, *, concurrency: int, capacity: int) -> None:
        with self._condition:
            self._concurrency = max(1, min(100, int(concurrency or 1)))
            self._capacity = max(1, int(capacity or 1))
            while len(self._workers) < self._concurrency:
                index = len(self._workers)
                worker = threading.Thread(
                    target=self._worker,
                    args=(index,),
                    daemon=True,
                    name=f"public-{self.name}-{index + 1}",
                )
                self._workers.append(worker)
                worker.start()
            self._condition.notify_all()

    def submit_many(self, tasks: list[Callable[[], Any]]) -> list[Future]:
        if not tasks:
            return []
        with self._condition:
            outstanding = self._running + len(self._queue)
            if outstanding + len(tasks) > self._concurrency + self._capacity:
                self._rejected += len(tasks)
                raise PublicTaskQueueFull(
                    f"{self.name} 队列已满（等待上限 {self._capacity}），请稍后重试"
                )
            futures = [Future() for _ in tasks]
            self._queue.extend(zip(futures, tasks))
            self._submitted += len(tasks)
            self._condition.notify_all()
            return futures

    def submit(self, task: Callable[[], Any]) -> Future:
        return self.submit_many([task])[0]

    def snapshot(self) -> dict:
        with self._condition:
            waiting = len(self._queue)
            outstanding = self._running + waiting
            return {
                "name": self.name,
                "waiting": waiting,
                "running": self._running,
                "capacity": self._capacity,
                "concurrency": self._concurrency,
                "available": max(0, self._concurrency + self._capacity - outstanding),
                "full": outstanding >= self._concurrency + self._capacity,
                "submitted": self._submitted,
                "completed": self._completed,
                "rejected": self._rejected,
            }

    def _worker(self, index: int) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: index < self._concurrency and bool(self._queue)
                )
                future, task = self._queue.popleft()
                self._running += 1
            try:
                if future.set_running_or_notify_cancel():
                    future.set_result(task())
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                with self._condition:
                    self._running -= 1
                    self._completed += 1
                    self._condition.notify_all()


QUOTA_TASKS = BoundedTaskDispatcher("额度查询", 512)
RELOGIN_TASKS = BoundedTaskDispatcher("401重登录", 128)


def configure_task_dispatchers(cfg: dict) -> None:
    concurrency = max(1, min(100, int(cfg.get("concurrency") or 1)))
    QUOTA_TASKS.configure(
        concurrency=concurrency,
        capacity=max(1, int(cfg.get("quota_queue_capacity") or 512)),
    )
    RELOGIN_TASKS.configure(
        concurrency=concurrency,
        capacity=max(1, int(cfg.get("relogin_queue_capacity") or 128)),
    )


def task_queue_status() -> dict:
    return {
        "quota": QUOTA_TASKS.snapshot(),
        "relogin": RELOGIN_TASKS.snapshot(),
    }


class ProxyLeasePool:
    """代理租取池，基于近期全局历史计数选取最少使用的代理（LRU 式）。

    每次创建都会从 proxy_lease_usage 表加载近期历史，即使逐请求创建
    也能正确选取全局最少使用的代理，避免每次都选中同一条。
    """

    def __init__(self, proxies: list[str], *, history_window: float = 10800):
        # 去重后再计数，否则同一 URL 重复出现会伪装成不同代理。
        self._proxies = list(dict.fromkeys(str(p or "").strip() for p in proxies if str(p or "").strip()))
        # 基于近期历史的租借计数（LRU 式），默认统计最近 3 小时。
        if self._proxies:
            since = time.time() - max(60, history_window)
            historical = db.proxy_lease_counts_since(self._proxies, since)
            self._usage = [historical.get(p, 0) for p in self._proxies]
        else:
            self._usage = []
        self._lock = threading.Lock()

    def lease(
        self,
        exclude_proxy: str = "",
        *,
        task_type: str = "",
        task_detail: str = "",
        skip_cooldown: bool = False,
    ) -> tuple[str, int, int]:
        """领取使用次数最少的代理；有其他选择时避开上一条失败代理。

        ``skip_cooldown`` 打开时跳过处于候选人任务冷却期的代理。该开关只给
        候选人路径使用——公开重登录等调用方共用本类，不应受此影响。若全部
        代理都在冷却，仍退回完整列表：宁可用坏代理也不能让整批任务停摆。
        """
        with self._lock:
            if not self._proxies:
                return "", -1, 0
            candidates = list(range(len(self._proxies)))
            if skip_cooldown:
                available = [
                    i for i in candidates
                    if not db.is_proxy_in_cooldown(
                        self._proxies[i], db.CANDIDATE_PROXY_ERROR_TYPE
                    )
                ]
                if available:
                    candidates = available
            alternatives = [i for i in candidates if self._proxies[i] != exclude_proxy]
            if alternatives:
                candidates = alternatives
            minimum = min(self._usage[i] for i in candidates)
            index = next(i for i in candidates if self._usage[i] == minimum)
            self._usage[index] += 1
            proxy = self._proxies[index]
            leased_count = self._usage[index]
        if task_type:
            proxy_usage.record_lease(proxy, task_type, task_detail)
        return proxy, index, leased_count

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {"proxy": proxy, "leased_count": self._usage[index]}
                for index, proxy in enumerate(self._proxies)
            ]


class _NoOtpMailProvider(MailProvider):
    kind = "public_relogin"
    display_name = "公开401重登（无邮箱OTP）"
    pooled = False
    ephemeral = False
    accepts_existing_account = True

    def __init__(self, email: str):
        self.email = str(email or "").strip().lower()

    def create_mailbox(self) -> str:
        return self.email

    def wait_for_otp(self, email_addr: str, timeout: int = 120, issued_after=None) -> str:
        raise RuntimeError(
            f"账号 {email_addr} 触发邮箱 OTP；公开重登页只支持密码 + 2FA，"
            "请补充可用邮箱取码信息后走系统内登录任务"
        )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _object(value: Any) -> dict:
    """将导入数据中的对象（包括 JSON 字符串）规范为 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _field(sources: list[dict], *names: str) -> Any:
    for source in sources:
        for name in names:
            value = source.get(name)
            if value not in (None, ""):
                return value
    return ""


def _lower_email(value: Any) -> str:
    return _text(value).lower()


def _jwt_auth(access_token: str) -> dict:
    return exporter._get_auth(exporter._decode_jwt_payload(access_token))


def _jwt_profile(access_token: str) -> dict:
    return exporter._get_profile(exporter._decode_jwt_payload(access_token))


def account_workspace_id(account: dict) -> str:
    raw = _object(account)
    credentials = _object(raw.get("credentials"))
    data = _object(raw.get("data"))
    extra = _object(raw.get("extra"))
    sources = [credentials, data, raw, extra]
    token = _text(_field(sources, "access_token", "accessToken", "access-token", "token"))
    auth = _jwt_auth(token)
    workspace_id = _field(sources, "workspace_id", "workspaceId", "chatgpt_account_id", "chatgptAccountId")
    return _text(
        workspace_id
        or extra.get("chatgpt_account_id")
        or auth.get("chatgpt_account_id")
        or auth.get("account_id")
        or _field(sources, "account_id", "accountId")
    )


def normalized_account(raw: dict) -> dict:
    raw = _object(raw)
    credentials = _object(raw.get("credentials"))
    data = _object(raw.get("data"))
    extra = _object(raw.get("extra"))
    sources = [credentials, data, raw, extra]
    access_token = _text(_field(sources, "access_token", "accessToken", "access-token", "token"))
    auth = _jwt_auth(access_token)
    profile = _jwt_profile(access_token)
    email = _lower_email(
        _field(sources, "email", "mail", "username", "name")
        or profile.get("email")
    )
    workspace_id = _text(
        _field(sources, "workspace_id", "workspaceId", "chatgpt_account_id", "chatgptAccountId")
        or auth.get("chatgpt_account_id")
        or auth.get("account_id")
        or _field(sources, "account_id", "accountId")
    )
    user_id = _text(
        _field(sources, "chatgpt_user_id", "chatgptUserId", "user_id", "userId")
        or auth.get("chatgpt_user_id")
        or auth.get("user_id")
    )
    plan_type = _text(_field(sources, "plan_type", "planType") or auth.get("chatgpt_plan_type") or "team")
    return {
        "email": email,
        "access_token": access_token,
        "refresh_token": _text(_field(sources, "refresh_token", "refreshToken")),
        "id_token": _text(_field(sources, "id_token", "idToken")),
        "session_token": _text(_field(sources, "session_token", "sessionToken")),
        "password": _text(_field(sources, "password", "passwd")),
        "totp_secret": _text(
            _field(sources, "totp_secret", "totpSecret", "two_factor_secret", "twoFactorSecret", "2fa")
        ),
        "chatgpt_account_id": workspace_id,
        "chatgpt_user_id": user_id,
        "client_id": _text(_field(sources, "client_id", "clientId") or exporter.CODEX_CLIENT_ID),
        "plan_type": plan_type or "team",
    }


def _headers(token: str, account_id: str, email: str = "") -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/",
        "User-Agent": "codex-cli",
        "oai-device-id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"public-relogin:{account_id or email or 'personal'}")),
    }
    # 个人账号的 Sub2API 导出通常没有 chatgpt_account_id；不发送空的
    # ChatGPT-Account-Id，让上游按 token 对应的个人账号处理。
    if account_id:
        headers["chatgpt-account-id"] = account_id
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def _looks_deactivated(text: str) -> bool:
    lower = str(text or "").lower()
    return any(
        marker in lower
        for marker in (
            "account deactivated",
            "account has been deactivated",
            "account disabled",
            "user_deactivated",
            "user disabled",
            "403",
            "account is not active",
        )
    )


def fetch_quota(account: dict, *, proxy: str = "", timeout: int = 30) -> dict:
    cred = normalized_account(account)
    token = cred["access_token"]
    workspace_id = cred["chatgpt_account_id"]
    if not token:
        raise ValueError("缺少 access_token")
    session = create_http_session(proxy=proxy or None)
    response = session.get(
        f"{BASE}/backend-api/wham/usage",
        headers=_headers(token, workspace_id, cred.get("email", "")),
        timeout=max(5, int(timeout or 30)),
    )
    if response.status_code >= 300:
        body = ""
        try:
            body = response.text[:1000]
        except Exception:
            body = ""
        if response.status_code == 401:
            raise PublicQuotaUnauthorized("额度查询失败 HTTP 401")
        if response.status_code == 403 or _looks_deactivated(body):
            raise PublicAccountDeactivated(f"账号停用/不可用 HTTP {response.status_code}")
        raise RuntimeError(f"额度查询失败 HTTP {response.status_code}: {body[:300]}")
    payload = response.json()
    rate = payload.get("rate_limit") or {}
    credits = payload.get("credits") or {}

    def window(key: str) -> dict:
        value = rate.get(key) or {}
        return {
            "used_percent": value.get("used_percent"),
            "window_seconds": value.get("limit_window_seconds"),
            "reset_at": value.get("reset_at"),
        }

    return {
        "plan_type": payload.get("plan_type") or "",
        "credits_balance": credits.get("balance"),
        "allowed": rate.get("allowed"),
        "primary": window("primary_window"),
        "secondary": window("secondary_window"),
        "updated_at": time.time(),
    }


def relogin_account(
    account: dict,
    *,
    proxy: str = "",
    login_timeout: int = 180,
    on_proxy_switch=None,
) -> dict:
    cred = normalized_account(account)
    email = cred["email"]
    password = cred["password"]
    totp_secret = cred["totp_secret"]
    workspace_id = cred["chatgpt_account_id"]
    if not email:
        raise ValueError("缺少 email")
    if not password:
        raise ValueError("缺少 password，无法执行密码登录")
    if not totp_secret:
        raise ValueError("缺少 totp_secret，无法执行 2FA 登录")

    cfg = Config(proxy=(proxy or "").strip() or None)

    def account_callback(_email: str) -> dict:
        return {"password": password, "totp_secret": totp_secret}

    flow = AuthFlow(
        cfg,
        env_overrides={
            "WEBUI_ALLOW_LOGIN": "1",
            "LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT": "1",
            "OTP_TIMEOUT": str(max(10, int(login_timeout or 180))),
            "OAUTH_CODEX_RT_EXCHANGE": "1",
            "OAUTH_CODEX_RT_BEFORE_CALLBACK": "1",
            "OAUTH_TOKEN_EXCHANGE_FROM_CALLBACK": "0",
        },
        account_callback=account_callback,
        on_proxy_switch=on_proxy_switch,
        workspace_id=workspace_id,
        personal_only=False,
    )
    flow.result.totp_secret = totp_secret
    result = flow.run_protocol_login(_NoOtpMailProvider(email), email, password=password)
    data = result.to_dict()
    data["email"] = email
    data["password"] = password
    data["totp_secret"] = totp_secret

    refreshed = normalized_account({**cred, **data})
    refreshed_workspace = refreshed.get("chatgpt_account_id") or workspace_id
    if workspace_id and refreshed_workspace != workspace_id:
        raise RuntimeError(f"重登后 workspace 不匹配: {refreshed_workspace} != {workspace_id}")
    return refreshed


def get_effective_config() -> dict:
    cfg = db.get_public_relogin_config()
    return {
        "enabled": str(cfg.get("enabled") or "0") in ("1", "true", "yes", "on"),
        "proxy_pool": cfg.get("proxy_pool") or "",
        "use_system_proxy_pool": str(cfg.get("use_system_proxy_pool") or "1").lower() in {"1", "true", "yes", "on"},
        "concurrency": max(1, min(100, int(cfg.get("concurrency") or 3))),
        "quota_queue_capacity": max(1, min(10000, int(cfg.get("quota_queue_capacity") or 512))),
        "relogin_queue_capacity": max(1, min(10000, int(cfg.get("relogin_queue_capacity") or 128))),
        "retry_count": max(0, min(5, int(cfg.get("retry_count") or 2))),
        "quota_timeout": max(5, min(120, int(cfg.get("quota_timeout") or 30))),
        "login_timeout": max(30, min(900, int(cfg.get("login_timeout") or 180))),
    }
