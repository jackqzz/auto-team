"""公开 401 重登录页的无落库业务逻辑。"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from config import Config
from auth_flow import AuthFlow
from http_client import create_http_session
from mail_providers.base import MailProvider

from . import db, exporter

BASE = "https://chatgpt.com"
logger = logging.getLogger("public_relogin")


class PublicQuotaUnauthorized(RuntimeError):
    pass


class PublicAccountDeactivated(RuntimeError):
    pass


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


def _lower_email(value: Any) -> str:
    return _text(value).lower()


def _jwt_auth(access_token: str) -> dict:
    return exporter._get_auth(exporter._decode_jwt_payload(access_token))


def _jwt_profile(access_token: str) -> dict:
    return exporter._get_profile(exporter._decode_jwt_payload(access_token))


def account_workspace_id(account: dict) -> str:
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else account
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    token = _text(credentials.get("access_token"))
    auth = _jwt_auth(token)
    return _text(
        credentials.get("chatgpt_account_id")
        or credentials.get("workspace_id")
        or credentials.get("account_id")
        or extra.get("workspace_id")
        or extra.get("chatgpt_account_id")
        or auth.get("chatgpt_account_id")
        or auth.get("account_id")
    )


def normalized_account(raw: dict) -> dict:
    credentials = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else raw
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    access_token = _text(credentials.get("access_token"))
    auth = _jwt_auth(access_token)
    profile = _jwt_profile(access_token)
    email = _lower_email(
        credentials.get("email")
        or raw.get("email")
        or raw.get("name")
        or extra.get("email")
        or profile.get("email")
    )
    workspace_id = _text(
        credentials.get("chatgpt_account_id")
        or credentials.get("workspace_id")
        or credentials.get("account_id")
        or extra.get("workspace_id")
        or auth.get("chatgpt_account_id")
        or auth.get("account_id")
    )
    user_id = _text(
        credentials.get("chatgpt_user_id")
        or credentials.get("user_id")
        or auth.get("chatgpt_user_id")
        or auth.get("user_id")
    )
    plan_type = _text(credentials.get("plan_type") or auth.get("chatgpt_plan_type") or "team")
    return {
        "email": email,
        "access_token": access_token,
        "refresh_token": _text(credentials.get("refresh_token")),
        "id_token": _text(credentials.get("id_token")),
        "session_token": _text(credentials.get("session_token") or raw.get("session_token")),
        "password": _text(credentials.get("password") or raw.get("password")),
        "totp_secret": _text(
            credentials.get("totp_secret")
            or credentials.get("two_factor_secret")
            or credentials.get("2fa")
            or raw.get("totp_secret")
        ),
        "chatgpt_account_id": workspace_id,
        "chatgpt_user_id": user_id,
        "client_id": _text(credentials.get("client_id") or exporter.CODEX_CLIENT_ID),
        "plan_type": plan_type or "team",
    }


def workspace_allowed(workspace_id: str, whitelist_text: str) -> bool:
    allowed = {
        line.strip()
        for line in str(whitelist_text or "").replace(",", "\n").splitlines()
        if line.strip()
    }
    return bool(workspace_id and workspace_id in allowed)


def _headers(token: str, account_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "ChatGPT-Account-Id": account_id,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/",
        "User-Agent": "codex-cli",
        "oai-device-id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"public-relogin:{account_id}")),
    }


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
    if not workspace_id:
        raise ValueError("无法解析 workspace_id/chatgpt_account_id")
    session = create_http_session(proxy=proxy or None)
    response = session.get(
        f"{BASE}/backend-api/wham/usage",
        headers=_headers(token, workspace_id),
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


def relogin_account(account: dict, *, proxy: str = "", login_timeout: int = 180) -> dict:
    cred = normalized_account(account)
    email = cred["email"]
    password = cred["password"]
    totp_secret = cred["totp_secret"]
    workspace_id = cred["chatgpt_account_id"]
    if not email:
        raise ValueError("缺少 email")
    if not workspace_id:
        raise ValueError("无法解析 workspace_id/chatgpt_account_id")
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
    if refreshed_workspace != workspace_id:
        raise RuntimeError(f"重登后 workspace 不匹配: {refreshed_workspace} != {workspace_id}")
    return refreshed


def get_effective_config() -> dict:
    cfg = db.get_public_relogin_config()
    return {
        "enabled": str(cfg.get("enabled") or "0") in ("1", "true", "yes", "on"),
        "workspace_whitelist": cfg.get("workspace_whitelist") or "",
        "proxy_pool": cfg.get("proxy_pool") or "",
        "concurrency": max(1, min(20, int(cfg.get("concurrency") or 3))),
        "retry_count": max(0, min(5, int(cfg.get("retry_count") or 2))),
        "quota_timeout": max(5, min(120, int(cfg.get("quota_timeout") or 30))),
        "login_timeout": max(30, min(900, int(cfg.get("login_timeout") or 180))),
    }
