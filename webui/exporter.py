"""导出注册凭证到 CPA / SUB2API 面板（路线 2 实现）。

参考 zc-zhangchen/any-auto-register 的 platforms/chatgpt/cpa_upload.py 和 sub2api_upload.py。

核心改造：
  ★ 可选地在导出前用 refresh_token 调 https://auth.openai.com/oauth/token 换新的
    Codex 风格 access_token（client_id=app_EMoamEEZ73f0CkXaXp7hrann）。
    新版流程会保存同源的 Codex AT/ID，因此默认直接导出即可；历史混合凭证可打开
    刷新开关，刷新成功后会把滚动后的 AT/RT/ID 回写数据库。

两种导出目标：
  1. CPA：multipart 文件上传 → POST /v0/management/auth-files
     Bearer 鉴权，文件名 {email}.json
  2. SUB2API：直接 POST /api/v1/admin/accounts
     x-api-key 鉴权（无登录流程）

全部用 curl_cffi impersonate="chrome110" 模拟浏览器 TLS 指纹，绕过 CF Bot 拦截。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# OpenAI / Codex 常量
OPENAI_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_SCOPE = "openid email profile offline_access"

# 默认值
DEFAULT_TIMEOUT = 30
DEFAULT_SUB2API_GROUP_IDS = [2]
SUB2API_DEFAULT_EXPIRES_IN = 863999  # 跟 any-auto-register 一致
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = [3.0, 7.0]

# ──────────────────────── 工具函数 ────────────────────────


def _decode_jwt_payload(token: str) -> dict:
    """解析 JWT payload 段。失败返回 {}。"""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        p = parts[1]
        pad = (4 - len(p) % 4) % 4
        data = json.loads(base64.urlsafe_b64decode(p + "=" * pad))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _b64url_json(d: dict) -> str:
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _get_auth(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    auth_info = payload.get("https://api.openai.com/auth")
    return auth_info if isinstance(auth_info, dict) else {}


def _get_profile(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    p = payload.get("https://api.openai.com/profile")
    return p if isinstance(p, dict) else {}


def _oidc_at_hash(access_token: str) -> str:
    """Return the OIDC ``at_hash`` value for an access token."""
    digest = hashlib.sha256(str(access_token or "").encode("utf-8")).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def validate_sub2_token_pair(access_token: str, id_token: str = "") -> None:
    """Reject a stale/mismatched access-token + ID-token pair.

    Sub2API can refresh a valid RT, but it cannot repair an ID token that was
    copied from a different access token.  OIDC's ``at_hash`` is the reliable
    local check and does not expose token contents in logs.
    """
    access = str(access_token or "").strip()
    identity = str(id_token or "").strip()
    if not access or not identity:
        return
    claims = _decode_jwt_payload(identity)
    claimed_hash = str(claims.get("at_hash") or "").strip()
    if claimed_hash and claimed_hash != _oidc_at_hash(access):
        raise RuntimeError("id_token 与 access_token 不匹配（at_hash 校验失败），请用同一 refresh_token 重新导出")


def _first(*values) -> str:
    for v in values:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _parse_group_ids(raw: Any, fallback: list[int] | None = None) -> list[int]:
    """支持字符串 '1,2'、列表、单值等格式，返回 list[int]。"""
    if isinstance(raw, str):
        candidates = [s.strip() for s in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    elif raw is None:
        candidates = []
    else:
        candidates = [raw]

    out: list[int] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            out.append(int(text))
        except ValueError:
            continue
    return out or list(fallback or DEFAULT_SUB2API_GROUP_IDS)


def _import_cffi():
    """惰性 import curl_cffi。失败抛 RuntimeError。"""
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests
    except ImportError as e:
        raise RuntimeError(f"curl_cffi 未安装，无法导出（pip install curl-cffi）: {e}")


def _import_cffi_mime():
    """惰性 import CurlMime。"""
    try:
        from curl_cffi import CurlMime
        return CurlMime
    except ImportError as e:
        raise RuntimeError(f"curl_cffi CurlMime 不可用: {e}")


# ──────────────────────── 核心：刷新 Codex access_token ────────────────────────


def refresh_codex_token(
    refresh_token: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    proxy: str = "",
    client_id: str = "",
) -> dict:
    """用 Codex refresh_token 换一组新的 access_token / id_token / refresh_token(滚动)。

    参考 any-auto-register/platforms/chatgpt/token_refresh.py 风格。

    返回 OpenAI 原始响应 dict：
        {access_token, refresh_token, id_token, expires_in, token_type}
    失败抛 RuntimeError。
    """
    rt = str(refresh_token or "").strip()
    if not rt:
        raise RuntimeError("缺少 refresh_token，无法刷新 Codex access_token")

    cffi = _import_cffi()
    refresh_client_id = str(client_id or CODEX_CLIENT_ID).strip() or CODEX_CLIENT_ID
    body = {
        "grant_type": "refresh_token",
        "client_id": refresh_client_id,
        "refresh_token": rt,
        "scope": CODEX_SCOPE,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://auth.openai.com",
        "Referer": "https://auth.openai.com/",
    }

    proxy_text = str(proxy or "").strip()
    proxies = {"http": proxy_text, "https": proxy_text} if proxy_text else None
    resp = cffi.post(
        OPENAI_TOKEN_ENDPOINT,
        headers=headers,
        data=body,
        proxies=proxies,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )

    if resp.status_code != 200:
        body_text = ""
        try:
            body_text = (resp.text or "")[:300]
        except Exception:
            pass
        raise RuntimeError(
            f"OpenAI token 刷新失败 HTTP {resp.status_code}: {body_text}"
        )

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError("OpenAI token 刷新返回非 JSON")

    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError(f"OpenAI token 刷新返回无 access_token: {str(data)[:200]}")

    return data


# ──────────────────────── CPA：生成 token JSON ────────────────────────


def _build_compat_id_token(*, access_token: str, email: str) -> str:
    """access_token 缺 id_token 时构造一个本地解析用的兼容 token。

    完全照 any-auto-register/cpa_upload.py:_build_compat_id_token 实现。
    注意：签名是固定字符串，仅供 CPA 等不校验签名的本地环境解析。
    """
    payload = _decode_jwt_payload(access_token)
    if not payload:
        return ""

    auth_info = _get_auth(payload)
    profile = _get_profile(payload)
    email_from_token = (profile.get("email") or payload.get("email") or email or "").strip()
    email_verified = bool(profile.get("email_verified", payload.get("email_verified", True)))
    account_id = str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()
    user_id = str(
        auth_info.get("chatgpt_user_id")
        or auth_info.get("user_id")
        or payload.get("sub")
        or ""
    ).strip()
    iat = int(payload.get("iat") or 0)
    exp = int(payload.get("exp") or 0)
    auth_time = int(payload.get("pwd_auth_time") or payload.get("auth_time") or iat or 0)
    session_id = str(
        payload.get("session_id")
        or f"compat_session_{(account_id or user_id or 'unknown').replace('-', '')[:24]}"
    ).strip()
    plan_type = str(auth_info.get("chatgpt_plan_type") or "free").strip() or "free"
    organization_id = str(
        auth_info.get("organization_id")
        or f"org-{hashlib.sha1((account_id or email_from_token or user_id).encode('utf-8')).hexdigest()[:24]}"
    )
    project_id = str(
        auth_info.get("project_id")
        or f"proj_{hashlib.sha1((organization_id + ':' + (account_id or user_id)).encode('utf-8')).hexdigest()[:24]}"
    )

    compat_auth = {
        "chatgpt_account_id": account_id,
        "chatgpt_plan_type": plan_type,
        "chatgpt_subscription_active_start": auth_info.get("chatgpt_subscription_active_start"),
        "chatgpt_subscription_active_until": auth_info.get("chatgpt_subscription_active_until"),
        "chatgpt_subscription_last_checked": auth_info.get("chatgpt_subscription_last_checked"),
        "chatgpt_user_id": user_id,
        "completed_platform_onboarding": bool(auth_info.get("completed_platform_onboarding", False)),
        "groups": auth_info.get("groups", []),
        "is_org_owner": bool(auth_info.get("is_org_owner", True)),
        "localhost": bool(auth_info.get("localhost", True)),
        "organization_id": organization_id,
        "organizations": auth_info.get("organizations") or [
            {"id": organization_id, "is_default": True, "role": "owner", "title": "Personal"}
        ],
        "project_id": project_id,
        "user_id": str(auth_info.get("user_id") or user_id or "").strip(),
    }

    compat_payload = {
        "amr": ["pwd", "otp", "mfa", "urn:openai:amr:otp_email"],
        "at_hash": hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:22],
        "aud": [CODEX_CLIENT_ID],
        "auth_provider": "password",
        "auth_time": auth_time,
        "email": email_from_token,
        "email_verified": email_verified,
        "exp": exp,
        "https://api.openai.com/auth": compat_auth,
        "iat": iat,
        "iss": payload.get("iss") or "https://auth.openai.com",
        "jti": f"compat-{hashlib.sha1(access_token.encode('utf-8')).hexdigest()[:32]}",
        "name": email_from_token or "OpenAI User",
        "rat": auth_time,
        "sid": session_id,
        "sub": payload.get("sub") or user_id,
    }

    header = {"alg": "RS256", "typ": "JWT", "kid": "compat"}
    signature = base64.urlsafe_b64encode(b"compat_signature_for_cpa_parsing_only").decode("ascii").rstrip("=")
    return f"{_b64url_json(header)}.{_b64url_json(compat_payload)}.{signature}"


def build_cpa_token_json(cred: dict) -> dict:
    """生成 CPA `/v0/management/auth-files` 的 multipart 文件内容。

    严格对齐 any-auto-register/cpa_upload.py:generate_token_json：
    8 个字段，UTC+8 时区。
    """
    access_token = str(cred.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("未读取到可导入的 access_token")
    refresh_token = str(cred.get("refresh_token") or "").strip()
    id_token = str(cred.get("id_token") or "").strip()
    email = str(cred.get("email") or "").strip()

    if not id_token:
        id_token = _build_compat_id_token(access_token=access_token, email=email)

    payload = _decode_jwt_payload(access_token)
    auth_info = _get_auth(payload)
    account_id = str(auth_info.get("chatgpt_account_id") or "").strip()

    tz_cn = timezone(timedelta(hours=8))
    expired_str = ""
    exp = payload.get("exp")
    if isinstance(exp, int) and exp > 0:
        expired_str = datetime.fromtimestamp(exp, tz=tz_cn).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    last_refresh = datetime.now(tz=tz_cn).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    return {
        "type": "codex",
        "email": email,
        "expired": expired_str,
        "id_token": id_token,
        "account_id": account_id,
        "access_token": access_token,
        "last_refresh": last_refresh,
        "refresh_token": refresh_token,
    }


# ──────────────────────── CPA：上传 ────────────────────────


def export_to_cpa(cred: dict, cfg: dict, *,
                    log_fn: Optional[Callable[[str, str], None]] = None) -> dict:
    """CPA multipart 上传。"""
    log = log_fn or (lambda m, lvl="info": logger.info(m))

    api_url = (cfg.get("cpa_url") or "").rstrip("/").strip()
    api_key = (cfg.get("cpa_mgmt_key") or "").strip()
    timeout = int(cfg.get("cpa_timeout") or DEFAULT_TIMEOUT)
    if not api_url:
        raise RuntimeError("CPA 未配置 URL")
    if not api_key:
        raise RuntimeError("CPA 未配置管理密钥")

    cffi = _import_cffi()
    CurlMime = _import_cffi_mime()

    token_data = build_cpa_token_json(cred)
    email = token_data.get("email") or "unknown"
    filename = f"{email}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")
    upload_url = f"{api_url}/v0/management/auth-files"
    # CLIProxyAPI 官方文档：两种 header 都接受。同时发以应对不同版本/部署的解析差异。
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Management-Key": api_key,
    }

    log(
        f"[CPA] 上传目标: {upload_url}  "
        f"文件名={filename}  内容大小={len(file_content)}B  "
        f"含 access_token={'是' if token_data.get('access_token') else '否'}  "
        f"含 refresh_token={'是' if token_data.get('refresh_token') else '否'}  "
        f"含 id_token={'是' if token_data.get('id_token') else '否'}",
        "info",
    )

    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        mime = None
        try:
            log(f"[CPA] 第 {attempt}/{MAX_ATTEMPTS} 次 multipart 上传 {filename}...", "info")
            mime = CurlMime()
            mime.addpart(
                name="file",
                data=file_content,
                filename=filename,
                content_type="application/json",
            )
            resp = cffi.post(
                upload_url,
                multipart=mime,
                headers=headers,
                proxies=None,
                verify=False,
                timeout=timeout,
                impersonate="chrome110",
            )
            # 详细日志：HTTP 状态 + 响应体
            try:
                body_preview = (resp.text or "")[:400]
            except Exception:
                body_preview = "(无法读取响应体)"
            log(
                f"[CPA] 服务器响应: HTTP {resp.status_code}  body={body_preview!r}",
                "info" if resp.status_code in (200, 201) else "warn",
            )
            if resp.status_code in (200, 201):
                log(f"[CPA] ✅ 上传成功 {filename}", "ok")
                return {"ok": True, "email": email, "file_name": filename,
                        "message": f"CPA 上传成功: {filename}"}
            msg = f"HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = str(detail.get("message") or detail.get("error") or detail.get("detail") or msg)
            except Exception:
                msg = f"{msg}: {body_preview}"
            last_err = msg
            # 失败时也打详细日志（即使 4xx 不重试，也要让主人看到原因）
            log(f"[CPA] ❌ 上传失败: {msg}", "error")
            if attempt < MAX_ATTEMPTS and resp.status_code >= 500:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(f"[CPA] 第 {attempt} 次失败 ({msg})，{delay:.0f}s 后重试", "warn")
                time.sleep(delay)
                continue
            return {"ok": False, "error": msg, "email": email, "file_name": filename}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(f"[CPA] 第 {attempt} 次异常 ({e})，{delay:.0f}s 后重试", "warn")
                time.sleep(delay)
                continue
            return {"ok": False, "error": str(e), "email": email, "file_name": filename}
        finally:
            if mime is not None:
                try:
                    mime.close()
                except Exception:
                    pass
    return {"ok": False, "error": last_err or "重试耗尽", "email": email, "file_name": filename}


# ──────────────────────── SUB2API：构建 payload ────────────────────────


def build_sub2api_payload(cred: dict, group_ids: list[int]) -> dict:
    """构建 SUB2API POST /api/v1/admin/accounts 的 body。

    严格对齐 any-auto-register/sub2api_upload.py:_build_sub2api_account_payload。
    """
    access_token = str(cred.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("未读取到可导入的 access_token")
    refresh_token = str(cred.get("refresh_token") or "").strip()
    id_token = str(cred.get("id_token") or "").strip()
    email = str(cred.get("email") or "").strip()
    validate_sub2_token_pair(access_token, id_token)

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _get_auth(access_payload)

    expires_at = access_payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= 0:
        expires_at = int(time.time()) + SUB2API_DEFAULT_EXPIRES_IN

    # organization_id 优先从 id_token 抽（更准），fallback 从 access_token
    id_auth = _get_auth(_decode_jwt_payload(id_token))
    organization_id = str(id_auth.get("organization_id") or "").strip()
    if not organization_id:
        orgs = id_auth.get("organizations") or []
        if isinstance(orgs, list):
            for o in orgs:
                if isinstance(o, dict):
                    organization_id = str(o.get("id") or "").strip()
                    if organization_id:
                        break
    if not organization_id:
        organization_id = str(access_auth.get("organization_id") or access_auth.get("poid") or "").strip()

    # The client_id embedded in the freshly issued access token is
    # authoritative; stale metadata must not select another RT family.
    client_id = str(
        access_payload.get("client_id") or cred.get("client_id") or CODEX_CLIENT_ID
    ).strip() or CODEX_CLIENT_ID

    chatgpt_account_id = str(
        access_auth.get("chatgpt_account_id") or cred.get("account_id") or ""
    ).strip()
    chatgpt_user_id = str(access_auth.get("chatgpt_user_id") or "").strip()

    # plan_type: 从 access_token claims 或 cred 取
    plan_type = str(
        access_auth.get("chatgpt_plan_type")
        or cred.get("plan_type")
        or cred.get("chatgpt_plan_type")
        or "free"
    ).strip() or "free"

    # workspace_id: 优先 chatgpt_account_id，fallback 从 cred
    workspace_id_value = chatgpt_account_id or str(
        cred.get("workspace_id") or cred.get("chatgpt_account_id") or ""
    ).strip()

    # session_token: 可能为空但字段需要存在
    session_token = str(cred.get("session_token") or "").strip()

    # expires_in: 用实际剩余秒数而非固定值
    expires_in = max(0, int(expires_at) - int(time.time()))
    expires_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace("+00:00", "Z")
    exported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    profile = _get_profile(access_payload)
    display_name = str(profile.get("name") or email or "").strip()
    account_user_id = ""
    if chatgpt_user_id and chatgpt_account_id:
        account_user_id = f"{chatgpt_user_id}__{chatgpt_account_id}"
    cred_extra = {
        "email": email,
        "source": "internal_resource_exchange",
        "privacy_mode": "training_off",
        "original_format": "codex-account",
        "openai_oauth_responses_websockets_v2_mode": "off",
        "openai_oauth_responses_websockets_v2_enabled": False,
    }
    live_identity = {
        "plan": plan_type,
        "email": email,
        "user_id": chatgpt_user_id,
        "client_id": client_id,
        "account_id": chatgpt_account_id,
        "plan_source": "oauth_access_token_claim",
        "verified_at": exported_at,
        "email_source": "oauth_userinfo_email",
        "official_plan": plan_type,
        "client_trusted": False,
        "email_verified": True,
        "user_id_source": "oauth_access_token_claim",
        "account_user_id": account_user_id,
        "identity_source": "oauth_access_token_claim",
        "account_id_source": "oauth_access_token_claim",
        "account_user_id_source": "oauth_access_token_claim",
    }

    return {
        "name": email,
        "type": "oauth",
        "extra": cred_extra,
        "platform": "openai",
        "priority": 1,
        "plan_type": plan_type,
        "concurrency": 10,
        "credentials": {
            "name": display_name,
            "type": "codex",
            "extra": cred_extra,
            "expired": expires_iso,
            "disabled": False,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "session_token": session_token,
            "expires_in": expires_in,
            "expires_at": expires_iso,
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "organization_id": organization_id,
            "client_id": client_id,
            "id_token": id_token,
            "plan_type": plan_type,
            "workspace_id": workspace_id_value,
            "account_id": chatgpt_account_id,
            "email": email,
            "email_source": "oauth_userinfo_email",
            "last_refresh": exported_at,
            "live_identity": live_identity,
            "outlook_email": email,
            "identity_source": "oauth_access_token_claim",
            "account_id_source": "oauth_access_token_claim",
            "chatgpt_plan_type": plan_type,
            "chatgpt_account_user_id": account_user_id,
        },
        "group_ids": list(group_ids) if group_ids else list(DEFAULT_SUB2API_GROUP_IDS),
        "auto_pause_on_expired": True,
        "expires_at": expires_at,
    }


# ──────────────────────── SUB2API：上传 ────────────────────────


def export_to_sub2api(cred: dict, cfg: dict, *,
                        log_fn: Optional[Callable[[str, str], None]] = None,
                        on_tokens_refreshed: Optional[Callable[[dict], None]] = None,
                        skip_token_refresh: bool = False) -> dict:
    """SUB2API x-api-key 直连上传（无登录流程）。

    当 cfg['refresh_oauth'] 为真时，用 refresh_token 换取新的 Codex 风格
    access_token；关闭时只使用现有凭证并做 AT/ID 一致性校验，不访问 OAuth 接口。

    skip_token_refresh: 当调用方（如 run_exports）已经完成刷新时置 True，避免重复刷新。
    """
    log = log_fn or (lambda m, lvl="info": logger.info(m))

    api_url = (cfg.get("sub2api_url") or "").rstrip("/").strip()
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    if not api_url:
        raise RuntimeError("SUB2API 未配置 URL")
    if not api_key:
        raise RuntimeError("SUB2API 未配置 API Key")

    # ── 可选刷新 Codex token ────────────────────────
    # 全局设置默认关闭；缺少该字段的调用也按关闭处理，避免在用户未打开
    # 开关时意外请求 OAuth。需要修复历史混合凭证时显式传 refresh_oauth=True。
    refresh_setting = cfg.get("refresh_oauth")
    if isinstance(refresh_setting, str):
        refresh_enabled = refresh_setting.strip().lower() in {"1", "true", "yes", "on"}
    else:
        refresh_enabled = False if refresh_setting is None else bool(refresh_setting)
    refresh_oauth = refresh_enabled and not skip_token_refresh
    rt = str(cred.get("refresh_token") or "").strip()
    if refresh_oauth and rt:
        try:
            refresh_timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
            proxy_text = str(cfg.get("proxy") or "").strip()
            log("[SUB2API] 用 refresh_token 换新的 Codex access_token...", "info")
            # Sub2/CLIProxy uses the official Codex OAuth client for RT
            # rotation. Do not fall back to the stale access token when this
            # refresh fails: that creates an AT/ID pair which Sub2 rejects.
            fresh = refresh_codex_token(
                rt,
                timeout=refresh_timeout,
                proxy=proxy_text,
                client_id=CODEX_CLIENT_ID,
            )
            validate_sub2_token_pair(
                fresh.get("access_token", ""),
                fresh.get("id_token", ""),
            )
            cred = {
                **cred,
                "access_token":  fresh["access_token"],
                "refresh_token": fresh.get("refresh_token") or cred.get("refresh_token"),
                "id_token":      fresh.get("id_token") or cred.get("id_token", ""),
            }
            log(
                f"[SUB2API] ✅ Codex token 刷新成功 "
                f"(access_token len={len(fresh['access_token'])} "
                f"id_token len={len(fresh.get('id_token') or '')})",
                "ok",
            )
            # 回写 DB
            if on_tokens_refreshed is not None:
                try:
                    on_tokens_refreshed(cred)
                except Exception as e:
                    log(f"[SUB2API] 新 OAuth token 回写 DB 失败（继续上传）: {e}", "warn")
        except Exception as e:
            raise RuntimeError(f"Sub2 导出前 refresh_token 刷新失败，已停止导出: {e}") from e
    else:
        validate_sub2_token_pair(
            cred.get("access_token", ""),
            cred.get("id_token", ""),
        )
        if refresh_oauth and not rt:
            log("[SUB2API] 已开启 OAuth 刷新但账号缺少 refresh_token，改用现有 access_token", "warn")
        else:
            log("[SUB2API] 未启用 OAuth 刷新，使用现有 access_token（已通过 AT/ID 一致性校验）", "info")

    group_ids = _parse_group_ids(cfg.get("sub2api_group_ids"))
    timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    payload = build_sub2api_payload(cred, group_ids)
    email = payload.get("name") or "unknown"
    url = f"{api_url}/api/v1/admin/accounts"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{api_url}/admin/accounts",
        "x-api-key": api_key,
    }

    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(
                f"[SUB2API] 第 {attempt}/{MAX_ATTEMPTS} 次上传 {email} "
                f"(group_ids={group_ids})...",
                "info",
            )
            resp = cffi.post(
                url,
                headers=headers,
                json=payload,
                proxies=None,
                verify=False,
                timeout=timeout,
                impersonate="chrome110",
            )
            if resp.status_code in (200, 201):
                new_id = ""
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        new_id = str(data.get("id") or data.get("ID") or "").strip()
                except Exception:
                    pass
                log(f"[SUB2API] ✅ 上传成功 {email} (id={new_id or 'unknown'})", "ok")
                return {"ok": True, "email": email, "account_id": new_id,
                        "message": f"SUB2API 上传成功 #{new_id or 'unknown'}"}
            msg = f"HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = str(
                        detail.get("message") or detail.get("msg")
                        or detail.get("error") or msg
                    )
            except Exception:
                msg = f"{msg} - {(resp.text or '')[:200]}"
            last_err = msg
            if attempt < MAX_ATTEMPTS and resp.status_code >= 500:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(f"[SUB2API] 第 {attempt} 次失败 ({msg})，{delay:.0f}s 后重试", "warn")
                time.sleep(delay)
                continue
            return {"ok": False, "error": msg, "email": email}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(f"[SUB2API] 第 {attempt} 次异常 ({e})，{delay:.0f}s 后重试", "warn")
                time.sleep(delay)
                continue
            return {"ok": False, "error": str(e), "email": email}
    return {"ok": False, "error": last_err or "重试耗尽", "email": email}


# ──────────────────────── 连通性测试 ────────────────────────


def test_cpa(cfg: dict) -> dict:
    """CPA 连通性测试：GET /v0/management/auth-files 真校验 Bearer key。

    用 GET 而不是 OPTIONS，因为 OPTIONS 是 CORS 预检，多数 CPA 实现不校验 Authorization，
    会让 key 错误的配置误以为通了，到真上传时才返 401。
    """
    api_url = (cfg.get("cpa_url") or "").rstrip("/").strip()
    api_key = (cfg.get("cpa_mgmt_key") or "").strip()
    if not api_url:
        raise RuntimeError("CPA 未配置 URL")
    if not api_key:
        raise RuntimeError("CPA 未配置管理密钥")
    timeout = int(cfg.get("cpa_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    resp = cffi.get(
        f"{api_url}/v0/management/auth-files",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Management-Key": api_key,
        },
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )
    if resp.status_code in (200, 201, 204):
        return {"ok": True, "message": f"CPA 连通正常 + 密钥有效 (HTTP {resp.status_code})"}
    if resp.status_code in (401, 403):
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        raise RuntimeError(
            f"CPA 鉴权失败 (HTTP {resp.status_code})：管理密钥错误。响应：{body}"
        )
    # 405 Method Not Allowed 表示路径对但不允许 GET，至少 URL 通了
    if resp.status_code == 405:
        return {"ok": True, "message": f"CPA URL 可达（HTTP 405），但无法用 GET 验证密钥；请实际上传一次确认"}
    raise RuntimeError(f"CPA 返回 HTTP {resp.status_code}: {(resp.text or '')[:200]}")


def test_sub2api(cfg: dict) -> dict:
    """SUB2API 连通性测试：GET 一个无害端点（用 admin/accounts list 验证 key）。"""
    api_url = (cfg.get("sub2api_url") or "").rstrip("/").strip()
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    if not api_url:
        raise RuntimeError("SUB2API 未配置 URL")
    if not api_key:
        raise RuntimeError("SUB2API 未配置 API Key")
    timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    resp = cffi.get(
        f"{api_url}/api/v1/admin/accounts",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{api_url}/admin/accounts",
            "x-api-key": api_key,
        },
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )
    if resp.status_code in (200, 201):
        return {"ok": True, "message": f"SUB2API 连通正常 (HTTP {resp.status_code})"}
    if resp.status_code in (401, 403):
        raise RuntimeError(f"SUB2API 鉴权失败 (HTTP {resp.status_code})，请检查 API Key")
    raise RuntimeError(f"SUB2API 返回 HTTP {resp.status_code}: {(resp.text or '')[:200]}")


# ──────────────────────── 统一入口（注册完成后调用） ────────────────────────


def run_exports(cred: dict, *,
                  cpa_cfg: Optional[dict] = None,
                  sub2api_cfg: Optional[dict] = None,
                  log_fn: Optional[Callable[[str, str], None]] = None,
                  on_tokens_refreshed: Optional[Callable[[dict], None]] = None,
                  refresh_oauth: Optional[bool] = None) -> dict:
    """注册完成后的可选导出入口。

    步骤：
      1. 检查两个目标是否有任一启用，全部未启用直接返回
      2. 按 refresh_oauth（未显式传入时跟随 sub2api_cfg）决定是否刷新一次
         Codex access_token / id_token
      3. 用当前或刷新后的 cred 走 CPA / SUB2API 导出

    返回：
        {"cpa": {...} 或 None, "sub2api": {...} 或 None, "any_attempted": bool}
    """
    log = log_fn or (lambda m, lvl="info": logger.info(m))
    out: dict = {"cpa": None, "sub2api": None, "any_attempted": False}

    cpa_on = bool(cpa_cfg and cpa_cfg.get("enabled"))
    sub2_on = bool(sub2api_cfg and sub2api_cfg.get("enabled"))
    if not (cpa_on or sub2_on):
        return out

    # 调用方显式传入时优先（例如自动任务的任务级开关）；普通手动调用则
    # 跟随全局 Sub2API 导出设置。CPA 单独导出永远不会因为该设置刷新 token。
    if refresh_oauth is not None:
        refresh_requested = bool(refresh_oauth)
    else:
        configured_refresh = (sub2api_cfg or {}).get("refresh_oauth")
        if isinstance(configured_refresh, str):
            refresh_requested = configured_refresh.strip().lower() in {"1", "true", "yes", "on"}
        else:
            refresh_requested = bool(configured_refresh)
    should_refresh = sub2_on and refresh_requested

    # 可选：用 refresh_token 换 Codex 风格 access_token。默认不刷新，
    # 直接使用注册结果中已有的 access_token，避免额外请求和地区限制。
    if should_refresh and str(cred.get("refresh_token") or "").strip():
      try:
        log("[exporter] 用 refresh_token 换新的 Codex access_token...", "info")
        refresh_cfg = sub2api_cfg or cpa_cfg or {}
        refresh_timeout = (
            refresh_cfg.get("cpa_timeout")
            or refresh_cfg.get("sub2api_timeout")
            or DEFAULT_TIMEOUT
        )
        fresh = refresh_codex_token(
            cred.get("refresh_token", ""),
            timeout=int(refresh_timeout),
            proxy=str(refresh_cfg.get("proxy") or ""),
            client_id=CODEX_CLIENT_ID,
        )
        cred = {
            **cred,
            "access_token":  fresh["access_token"],
            "refresh_token": fresh.get("refresh_token") or cred.get("refresh_token"),
            "id_token":      fresh.get("id_token") or cred.get("id_token", ""),
        }
        if on_tokens_refreshed is not None:
            try:
                on_tokens_refreshed(cred)
            except Exception as e:
                log(f"[exporter] 新 OAuth token 回写失败（继续上传）: {e}", "warn")
        log(
            f"[exporter] ✅ Codex token 刷新成功 "
            f"(access_token len={len(fresh['access_token'])} "
            f"id_token len={len(fresh.get('id_token') or '')})",
            "ok",
        )
      except Exception as e:
        log(f"[exporter] ❌ Codex token 刷新失败，无法导出: {e}", "error")
        if cpa_on:
            out["any_attempted"] = True
            out["cpa"] = {"ok": False, "error": f"Codex token 刷新失败: {e}"}
        if sub2_on:
            out["any_attempted"] = True
            out["sub2api"] = {"ok": False, "error": f"Codex token 刷新失败: {e}"}
        return out
    elif should_refresh:
        log("[exporter] 已开启 OAuth 刷新，但账号缺少 refresh_token，跳过刷新并继续导出", "warn")
    else:
        log("[exporter] 已关闭 OAuth 刷新，直接使用现有 access_token", "info")

    if cpa_on:
        out["any_attempted"] = True
        try:
            out["cpa"] = export_to_cpa(cred, cpa_cfg, log_fn=log)
        except Exception as e:
            log(f"[CPA] 导出异常: {e}", "error")
            out["cpa"] = {"ok": False, "error": str(e)}

    if sub2_on:
        out["any_attempted"] = True
        try:
            # 把解析后的最终开关写入本次调用副本，避免调用方显式传 False
            # 时被 cfg 中的全局 True 再次触发内部刷新。
            sub2_export_cfg = dict(sub2api_cfg or {})
            sub2_export_cfg["refresh_oauth"] = should_refresh
            out["sub2api"] = export_to_sub2api(
                cred, sub2_export_cfg, log_fn=log,
                on_tokens_refreshed=on_tokens_refreshed,
                # should_refresh=True 时上面已经刷新过了，skip 避免重复刷新
                skip_token_refresh=should_refresh,
            )
        except Exception as e:
            log(f"[SUB2API] 导出异常: {e}", "error")
            out["sub2api"] = {"ok": False, "error": str(e)}

    return out


def push_many_to_cpa(
    rows: list[dict], cpa_cfg: dict,
    on_tokens_refreshed: Optional[Callable[[dict], None]] = None,
    proxy: str = "",
) -> list[dict]:
    """批量推送 CPA；每个账号独立执行，单条失败不终止后续账号。"""
    results = []
    cfg = dict(cpa_cfg or {})
    cfg["enabled"] = True
    if proxy:
        cfg["proxy"] = proxy
    for cred in rows:
        email = str(cred.get("email") or "").strip().lower()
        try:
            exported = run_exports(
                cred, cpa_cfg=cfg, sub2api_cfg=None,
                on_tokens_refreshed=on_tokens_refreshed,
            )
            result = exported.get("cpa") or {"ok": False, "error": "CPA 未执行"}
            results.append({"email": email, **result})
        except Exception as e:
            results.append({"email": email, "ok": False, "error": str(e)})
    return results
