"""Team 空间候选人的真实邀请/申请及只读席位同步。"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path

from http_client import create_http_session
from .workspace_client import create_workspace_http_session
from . import db

BASE = "https://chatgpt.com"
WORKSPACE_ADMIN_REQUEST_INTERVAL_SECONDS = 1.0
WORKSPACE_ADMIN_MAX_429_RETRIES = 3
# 连续多少轮 403 才判定账号停用。单次 403 常是边缘节点瞬时拒绝，直接判死
# 会误杀仍在正常使用的账号。
DEACTIVATION_403_STREAK = 2

_workspace_admin_state_lock = threading.Lock()
_workspace_admin_locks: dict[int, threading.Lock] = {}
_workspace_admin_last_completed: dict[int, float] = {}
_workspace_admin_cooldown_until: dict[int, float] = {}

class QuotaUnauthorized(RuntimeError):
    """额度接口明确返回 401。"""
    pass

class QuotaHttpError(RuntimeError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"额度查询失败 HTTP {status_code}")


class QuotaNetworkError(RuntimeError):
    """网络/TLS/超时/5xx —— 可重试类失败，重试次数耗尽后才抛出。"""


class QuotaAccountDeactivated(RuntimeError):
    """连续多轮 403，判定账号已停用。

    单次 403 常常是边缘节点/风控的瞬时拒绝（日志里多次出现同一秒内跨账号
    成组 403），因此必须连续命中才归因到账号本身。
    """
    def __init__(self, message: str, *, streak: int = 0):
        self.streak = int(streak)
        super().__init__(message)


class QuotaPaymentRequired(RuntimeError):
    """402 —— 母号空间订阅/计费异常，不归因到单个候选账号。"""


class UpstreamHttpError(RuntimeError):
    def __init__(self, status_code: int, detail: object):
        self.status_code = int(status_code)
        super().__init__(f"上游 HTTP {self.status_code}: {detail}")

def fetch_candidate_quota(
    workspace_db_id: int,
    email: str,
    *,
    proxy: str,
    network_retries: int = 2,
) -> dict:
    """查询候选人的 Codex 额度，且必须使用本次从候选人代理池租取的代理。

    额度请求携带的是候选人的 Team Access Token，不属于母号管理请求，禁止
    复用 ``workspace_masters.proxy_url``。调用方必须显式传入代理，避免配置
    缺失时悄悄回退到母号出口或直连。

    ``network_retries`` 控制网络异常（TLS 握手失败、连接超时等）与 5xx 的
    重试次数，两者共用同一份预算；429 另有独立预算并按 ``Retry-After`` 退避。
    传输层失败会累计到该代理的熔断连击上，拿到任何响应则清零。
    """
    proxy_value = str(proxy or "").strip()
    if not proxy_value:
        raise ValueError("候选人代理池为空，额度查询无法租取代理")
    master = db.get_workspace_master(workspace_db_id)
    if not master:
        raise RuntimeError("母号不存在")
    session = create_http_session(proxy=proxy_value)
    rows = db.list_workspace_credentials_by_emails(workspace_db_id, [email])
    if not rows: raise RuntimeError("候选人尚未获取当前空间凭证")
    cred = rows[0]; token = cred.get("access_token") or ""; wid = master.get("workspace_id") or ""
    try:
        prior_quota = json.loads(cred.get("quota_json") or "{}")
        if not isinstance(prior_quota, dict):
            prior_quota = {}
    except Exception:
        prior_quota = {}

    net_left = max(0, int(network_retries or 0))
    rate_left = max(0, int(WORKSPACE_ADMIN_MAX_429_RETRIES or 0))
    net_attempt = 0
    rate_attempt = 0
    response = None
    while True:
        try:
            response = session.get(f"{BASE}/backend-api/wham/usage", headers={**_headers(token, wid), "ChatGPT-Account-Id": wid, "User-Agent": "codex-cli"}, timeout=30)
        except Exception as exc:
            if net_left <= 0:
                # 只有传输层失败才归因到代理：拿到任何 HTTP 响应（哪怕 5xx）
                # 都说明这条代理是通的，不该计入熔断连击。
                streak = db.record_candidate_proxy_failure(proxy_value)
                logger.warning(
                    "额度查询代理传输失败 proxy=%s streak=%s/%s workspace_db_id=%s email=%s",
                    proxy_value, streak, db.CANDIDATE_PROXY_FAILURE_STREAK,
                    workspace_db_id, email,
                )
                raise QuotaNetworkError(
                    f"额度查询网络错误（已重试{net_attempt}次）：{exc}"
                ) from exc
            net_left -= 1
            net_attempt += 1
            delay = min(10.0, float(net_attempt))
            logger.warning(
                "额度查询网络异常，将重试 workspace_db_id=%s email=%s attempt=%s wait=%.1fs error=%s",
                workspace_db_id, email, net_attempt, delay, str(exc)[:180],
            )
            time.sleep(delay)
            continue

        # 5xx 与网络异常同源（多为出口/边缘抖动），共用同一份重试预算。
        if response.status_code >= 500 and net_left > 0:
            net_left -= 1
            net_attempt += 1
            delay = min(10.0, float(net_attempt))
            logger.warning(
                "额度查询上游 %s，将重试 workspace_db_id=%s email=%s attempt=%s wait=%.1fs",
                response.status_code, workspace_db_id, email, net_attempt, delay,
            )
            time.sleep(delay)
            continue

        if response.status_code == 429 and rate_left > 0:
            rate_left -= 1
            delay = _retry_after_seconds(response, rate_attempt)
            rate_attempt += 1
            logger.warning(
                "额度查询触发限流，退避重试 workspace_db_id=%s email=%s attempt=%s wait=%.1fs",
                workspace_db_id, email, rate_attempt, delay,
            )
            time.sleep(delay)
            continue
        break

    # 拿到响应即证明代理链路可用，清零连击（业务层错误另有各自的处理）。
    db.clear_candidate_proxy_failure(proxy_value)

    if response.status_code >= 300:
        code = int(response.status_code)
        record = {"error_code": code, "updated_at": time.time()}
        # 403 连击计数只在 403 分支写入；其余任何结果（含成功）落库时都不带
        # 该键，等同于自动清零。
        streak = 0
        if code == 403:
            streak = max(0, int(prior_quota.get("consecutive_403") or 0)) + 1
            record["consecutive_403"] = streak
        db.update_workspace_quota(workspace_db_id, email, record)
        db.update_workspace_candidate_status(workspace_db_id, email, f"quota_error_{code}")
        if code == 401:
            raise QuotaUnauthorized("额度查询失败 HTTP 401")
        if code == 402:
            raise QuotaPaymentRequired(
                f"母号空间计费异常 HTTP 402：{_response_debug_body(response)[:300]}"
            )
        if code == 403:
            logger.warning(
                "额度查询 403 workspace_db_id=%s email=%s streak=%s/%s body=%s",
                workspace_db_id, email, streak, DEACTIVATION_403_STREAK,
                _response_debug_body(response)[:300],
            )
            if streak >= DEACTIVATION_403_STREAK:
                raise QuotaAccountDeactivated(
                    f"连续 {streak} 次额度查询 403，判定账号停用", streak=streak
                )
            raise QuotaHttpError(code)
        if code >= 500:
            raise QuotaNetworkError(f"额度查询失败 HTTP {code}（已重试{net_attempt}次）")
        raise QuotaHttpError(code)
    payload = response.json(); rate = payload.get("rate_limit") or {}; credits = payload.get("credits") or {}
    def window(key):
        w = rate.get(key) or {}; return {"used_percent": w.get("used_percent"), "window_seconds": w.get("limit_window_seconds"), "reset_at": w.get("reset_at")}
    result = {"plan_type": payload.get("plan_type") or "", "credits_balance": credits.get("balance"), "allowed": rate.get("allowed"), "primary": window("primary_window"), "secondary": window("secondary_window"), "updated_at": time.time()}
    db.update_workspace_quota(workspace_db_id, email, result)
    return result
logger = logging.getLogger("workspace_membership")
if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "").endswith("workspace-membership.log") for h in logger.handlers):
    _log_dir = Path(__file__).resolve().parent / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _handler = logging.FileHandler(_log_dir / "workspace-membership.log", encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def _workspace_admin_lock(workspace_db_id: int) -> threading.Lock:
    key = int(workspace_db_id)
    with _workspace_admin_state_lock:
        lock = _workspace_admin_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _workspace_admin_locks[key] = lock
        return lock


def _retry_after_seconds(response, attempt: int) -> float:
    raw = response.headers.get("Retry-After") if getattr(response, "headers", None) else None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = 0.0
        if raw:
            try:
                parsed = parsedate_to_datetime(str(raw))
                delay = parsed.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                delay = 0.0
        if delay <= 0:
            delay = min(30.0, 2.0 * (2 ** max(0, attempt)))
    return max(1.0, min(60.0, delay))


def _workspace_admin_request(
    workspace_db_id: int,
    session,
    method: str,
    url: str,
    *,
    request_interval: float = WORKSPACE_ADMIN_REQUEST_INTERVAL_SECONDS,
    max_429_retries: int = WORKSPACE_ADMIN_MAX_429_RETRIES,
    network_retries: int = 2,
    **kwargs,
):
    """串行并节流同一母号的管理 API 请求，并对 429 做共享退避。"""
    key = int(workspace_db_id)
    interval = max(0.0, float(request_interval or 0.0))
    retries = max(0, int(max_429_retries or 0))
    request = getattr(session, str(method).strip().lower())
    auth_refreshed = False

    with _workspace_admin_lock(key):
        for attempt in range(retries + 1):
            now = time.monotonic()
            with _workspace_admin_state_lock:
                earliest = max(
                    _workspace_admin_last_completed.get(key, 0.0) + interval,
                    _workspace_admin_cooldown_until.get(key, 0.0),
                )
            wait_seconds = max(0.0, earliest - now)
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            try:
                response = request(url, **kwargs)
            except Exception as exc:
                text = str(exc).lower()
                retryable = any(marker in text for marker in (
                    "failed to connect", "could not connect", "connection", "timed out", "timeout",
                ))
                if retryable and attempt < max(0, int(network_retries or 0)):
                    delay = min(10.0, 1.0 + float(attempt))
                    logger.warning(
                        "母号管理请求网络异常，将重试 workspace_db_id=%s method=%s attempt=%s/%s wait=%.1fs error=%s",
                        key, str(method).upper(), attempt + 1,
                        max(1, int(network_retries or 0) + 1), delay, str(exc)[:180],
                    )
                    time.sleep(delay)
                    continue
                logger.error(
                    "母号专属代理不可用 workspace_db_id=%s method=%s proxy_source=workspace_masters.proxy_url error=%s",
                    key, str(method).upper(), str(exc)[:240],
                )
                raise
            finally:
                with _workspace_admin_state_lock:
                    _workspace_admin_last_completed[key] = time.monotonic()

            if response.status_code == 401 and not auth_refreshed:
                refreshed = _refresh_workspace_access_token(key, session)
                if refreshed:
                    auth_refreshed = True
                    current_headers = dict(kwargs.get("headers") or {})
                    current_headers.update(_headers(refreshed, _workspace_external_id(key, fallback="")))
                    # 保留调用方特有的 Referer/其他头，仅替换失效 Authorization
                    # 和 account id。
                    current_headers["Authorization"] = f"Bearer {refreshed}"
                    kwargs["headers"] = current_headers
                    logger.info(
                        "母号管理请求已用 session token 刷新 access_token workspace_db_id=%s method=%s url=%s",
                        key,
                        str(method).upper(),
                        str(url).split("?")[0],
                    )
                    continue
                logger.warning(
                    "母号管理请求鉴权失败 workspace_db_id=%s method=%s status=401 url=%s body=%s",
                    key,
                    str(method).upper(),
                    str(url).split("?")[0],
                    _response_debug_body(response),
                )

            if response.status_code != 429:
                with _workspace_admin_state_lock:
                    _workspace_admin_cooldown_until.pop(key, None)
                return response

            delay = _retry_after_seconds(response, attempt)
            with _workspace_admin_state_lock:
                _workspace_admin_cooldown_until[key] = time.monotonic() + delay
            logger.warning(
                "母号管理请求触发限流 workspace_db_id=%s method=%s attempt=%s/%s wait=%.1fs",
                key,
                str(method).upper(),
                attempt + 1,
                retries + 1,
                delay,
            )
            if attempt >= retries:
                return response

    raise RuntimeError("母号管理请求未返回响应")


def _safe_body(value) -> str:
    """日志摘要：保留错误定位信息，去除常见凭证字段并限制长度。"""
    try:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    except Exception:
        text = str(value)
    text = re.sub(r'(?i)(bearer\s+)[^\s"}]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)("?(?:accessToken|access_token|sessionToken|session_token|cookie|cookie_header)"?\s*[:=]\s*")[^"]+', r'\1<redacted>', text)
    return text[:1200]


def _response_debug_body(response) -> str:
    """安全读取响应摘要，日志分支不能因非 JSON 响应再次抛异常。"""
    try:
        value = response.json() if hasattr(response, "json") else getattr(response, "text", "")
    except Exception:
        value = getattr(response, "text", "")
    return _safe_body(value)


def _workspace_external_id(workspace_db_id: int, fallback: str = "") -> str:
    """返回母号的远端 Workspace ID；仅用于刷新凭证/构造请求头。"""
    master = db.get_workspace_master(int(workspace_db_id)) or {}
    return str(master.get("workspace_id") or fallback or "").strip()


def _workspace_device_id(account_id: str) -> str:
    """为管理端请求生成稳定的浏览器设备标识。"""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"workspace-admin:{account_id or 'unknown'}"))


def _refresh_workspace_access_token(workspace_db_id: int, session) -> str:
    """用母号保存的 NextAuth session cookie 换取新的 access token。"""
    master = db.get_workspace_master(int(workspace_db_id)) or {}
    session_token = str(master.get("session_token") or "").strip()
    workspace_id = str(master.get("workspace_id") or "").strip()
    if not session_token or not workspace_id:
        logger.warning(
            "母号 access_token 已失效且缺少 session_token/workspace_id，无法刷新 workspace_db_id=%s",
            workspace_db_id,
        )
        return ""
    try:
        session.cookies.set(
            "__Secure-next-auth.session-token",
            session_token,
            domain=".chatgpt.com",
            path="/",
        )
        response = session.get(
            f"{BASE}/api/auth/session",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": BASE,
                "Referer": f"{BASE}/admin/members",
                "oai-device-id": _workspace_device_id(workspace_id),
            },
            timeout=30,
        )
        if response.status_code < 200 or response.status_code >= 300:
            logger.warning(
                "母号 session 刷新请求失败 workspace_db_id=%s status=%s body=%s",
                workspace_db_id,
                response.status_code,
                _response_debug_body(response),
            )
            return ""
        data = response.json() if hasattr(response, "json") else {}
        access_token = str((data or {}).get("accessToken") or (data or {}).get("access_token") or "").strip()
        if not access_token:
            logger.warning(
                "母号 session 刷新响应未返回 accessToken workspace_db_id=%s body=%s",
                workspace_db_id,
                _safe_body(data),
            )
            return ""
        token_account = str(
            (_payload(access_token).get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
            or ""
        ).strip()
        if token_account and token_account != workspace_id:
            logger.warning(
                "母号 session 刷新返回了其他 Workspace 的 accessToken workspace_db_id=%s expected=%s actual=%s",
                workspace_db_id,
                workspace_id,
                token_account,
            )
            return ""
        new_session_token = ""
        try:
            cookie_value = session.cookies.get("__Secure-next-auth.session-token")
            if isinstance(cookie_value, str) and cookie_value.strip():
                new_session_token = cookie_value.strip()
        except Exception:
            new_session_token = ""
        db.update_workspace_master_auth(
            workspace_db_id,
            access_token,
            new_session_token or None,
        )
        return access_token
    except Exception:
        logger.exception("母号 session 刷新异常 workspace_db_id=%s", workspace_db_id)
        return ""


def _payload(token: str) -> dict:
    try:
        part = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except Exception:
        return {}


def _headers(token: str, account_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/admin/members",
        "oai-device-id": _workspace_device_id(account_id),
    }


def _json(response):
    try: data = response.json()
    except Exception: data = {"message": (response.text or "")[:500]}
    if response.status_code < 200 or response.status_code >= 300:
        logger.warning("上游请求失败 status=%s body=%s", response.status_code, _safe_body(data))
        raise UpstreamHttpError(
            response.status_code,
            data.get("message") or data.get("error") or data,
        )
    if isinstance(data, dict) and data.get("error"):
        logger.warning("上游返回 error body=%s", _safe_body(data))
        raise RuntimeError(str(data["error"]))
    return data


def invite_candidates(workspace_db_id: int, emails: list[str], seat_type: str = "default") -> dict:
    session, master = create_workspace_http_session(workspace_db_id)
    workspace_id = master.get("workspace_id") or ""
    token = master.get("access_token") or ""
    if not workspace_id or not token:
        raise RuntimeError("母号缺少 Workspace ID 或 Access Token，请重新导入完整 session.json")
    if seat_type not in {"default", "usage_based", "prolite"}:
        raise ValueError("席位类型只能是标准席位、Usage-based 或 ProLite")
    count = len(emails)
    req_timeout = max(30, min(300, 20 + count * 2))
    logger.info("母号批量邀请开始 workspace_db_id=%s workspace_id=%s seat_type=%s count=%s timeout=%ss emails=%s", workspace_db_id, workspace_id, seat_type, count, req_timeout, emails[:20])
    try:
        response = _workspace_admin_request(
            workspace_db_id,
            session,
            "post",
            f"{BASE}/backend-api/accounts/{workspace_id}/invites",
            headers=_headers(token, workspace_id),
            json={"email_addresses": emails, "role": "standard-user", "seat_type": seat_type, "resend_emails": True},
            timeout=req_timeout,
        )
    except Exception:
        logger.exception("母号批量邀请网络异常 workspace_db_id=%s workspace_id=%s", workspace_db_id, workspace_id)
        raise
    logger.info("母号批量邀请响应 workspace_db_id=%s status=%s", workspace_db_id, response.status_code)
    return _json(response)


def check_candidate_membership(
    workspace_db_id: int,
    emails: list[str],
    *,
    prefer_invites: bool = False,
    include_seats: bool = False,
) -> dict[str, str] | tuple[dict[str, str], dict[str, dict[str, str]]]:
    """通过 invites/users 确认状态和席位；同一母号的查询严格串行并节流。"""
    session, master = create_workspace_http_session(workspace_db_id)
    workspace_id, token = master.get("workspace_id") or "", master.get("access_token") or ""
    if not workspace_id or not token:
        raise RuntimeError("母号缺少 Workspace ID 或 Access Token")
    wanted = list(dict.fromkeys(str(e).strip().lower() for e in emails if str(e).strip()))
    states = {email: "not_invited" for email in wanted}
    seats: dict[str, dict[str, str]] = {}
    member_errors: dict[str, Exception] = {}
    invite_error: Exception | None = None
    headers = _headers(token, workspace_id)

    def check_members(targets: list[str]) -> None:
        for email in targets:
            try:
                response = _workspace_admin_request(
                    workspace_db_id,
                    session,
                    "get",
                    f"{BASE}/backend-api/accounts/{workspace_id}/users",
                    params={"offset": 0, "limit": 25, "query": email}, headers=headers, timeout=30,
                )
                users = _json(response)
                items = users.get("items", []) if isinstance(users, dict) else []
                member = next(
                    (
                        item for item in items
                        if isinstance(item, dict)
                        and str(item.get("email") or "").strip().lower() == email
                    ),
                    None,
                )
                if member:
                    states[email] = "joined"
                    seats[email] = _candidate_seat_snapshot(member)
            except Exception as exc:
                member_errors[email] = exc
                logger.exception(
                    "查询母号成员失败 workspace_db_id=%s workspace_id=%s email=%s",
                    workspace_db_id,
                    workspace_id,
                    email,
                )
                if isinstance(exc, UpstreamHttpError) and exc.status_code == 429:
                    raise

    def check_invites(targets: list[str]) -> None:
        nonlocal invite_error
        remaining = set(targets)
        if not remaining:
            return
        try:
            def invite_seat_type(value: dict) -> str:
                # 邀请接口不同版本使用过 snake_case / camelCase；保留常见
                # 别名，避免待接受邀请的席位信息因字段命名变化而丢失。
                for key in (
                    "seat_type",
                    "seatType",
                    "seat",
                    "workspace_seat_type",
                    "workspaceSeatType",
                ):
                    raw = value.get(key)
                    if isinstance(raw, dict):
                        raw = raw.get("type") or raw.get("value") or raw.get("name")
                    if str(raw or "").strip():
                        return str(raw).strip()
                return ""

            def walk(value):
                if isinstance(value, dict):
                    candidates = [value.get("email"), value.get("email_address"), value.get("invitee_email"), value.get("recipient_email")]
                    candidates += [((value.get("user") or {}).get("email") if isinstance(value.get("user"), dict) else ""), ((value.get("invitee") or {}).get("email") if isinstance(value.get("invitee"), dict) else "")]
                    addresses = value.get("email_addresses")
                    if isinstance(addresses, list):
                        candidates.extend(addresses)
                    matched = {str(email).strip().lower() for email in candidates if str(email or "").strip().lower() in remaining}
                    if matched:
                        # 当前接口约定：status=2 为待接受邀请；status=1 为待处理申请。
                        raw_status = value.get("status")
                        state = "pending_invite" if str(raw_status) == "2" else ("pending_request" if str(raw_status) == "1" else "pending_invite")
                        for email in matched:
                            states[email] = state
                            raw_seat_type = invite_seat_type(value)
                            if raw_seat_type:
                                # 邀请记录没有 member_id，不能套用成员快照中的
                                # id 字段；这里只返回席位信息，供候选表持久化展示。
                                seats[email] = _candidate_seat_snapshot({"seat_type": raw_seat_type})
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            offset = 0
            limit = 100
            while True:
                response = _workspace_admin_request(
                    workspace_db_id,
                    session,
                    "get",
                    f"{BASE}/backend-api/accounts/{workspace_id}/invites",
                    params={"include_pending": "true", "include_requests": "true", "offset": offset, "limit": limit, "query": ""},
                    headers=headers,
                    timeout=30,
                )
                pending = _json(response)
                logger.info(
                    "邀请列表状态校验响应 workspace_db_id=%s offset=%s body=%s",
                    workspace_db_id,
                    offset,
                    _safe_body(pending),
                )
                walk(pending)
                if all(states[email] != "not_invited" for email in remaining):
                    break
                items = pending.get("items", []) if isinstance(pending, dict) else []
                try:
                    total = max(0, int(pending.get("total") or 0)) if isinstance(pending, dict) else 0
                except (TypeError, ValueError):
                    total = 0
                received = len(items) if isinstance(items, list) else 0
                if received <= 0 or received < limit:
                    break
                offset += received
                if total and offset >= total:
                    break
        except Exception as exc:
            invite_error = exc
            logger.exception("查询母号邀请列表失败 workspace_db_id=%s workspace_id=%s", workspace_db_id, workspace_id)
            if isinstance(exc, UpstreamHttpError) and exc.status_code == 429:
                raise

    if prefer_invites:
        # 批量邀请后的常见结果是 pending_invite。先用一次列表请求确认整批，
        # 仅对未命中的邮箱查询 members，避免 N 个紧邻的 `/users?query=` 请求。
        check_invites(wanted)
        check_members([email for email in wanted if states[email] == "not_invited"])
    else:
        # 手动校验优先确认 joined，避免历史邀请记录覆盖真实成员状态。
        check_members(wanted)
        check_invites([email for email in wanted if states[email] != "joined"])

    unknown = [
        email for email in wanted
        if states[email] == "not_invited"
        and (email in member_errors or invite_error is not None)
    ]
    if unknown:
        upstream_error = next(
            (
                error for error in [invite_error, *(member_errors.get(email) for email in unknown)]
                if isinstance(error, UpstreamHttpError)
            ),
            None,
        )
        if upstream_error is not None:
            raise upstream_error
        raise RuntimeError(
            f"候选状态校验受上游限流/请求错误影响，{len(unknown)} 个状态无法确认"
        ) from invite_error

    logger.info("候选成员状态校验 workspace_db_id=%s states=%s", workspace_db_id, states)
    if include_seats:
        return states, seats
    return states

def _candidate_seat_snapshot(item: dict) -> dict[str, str]:
    raw_seat = str(item.get("seat_type") or item.get("seatType") or item.get("seat") or "").strip()
    seat = _canonical_candidate_seat_type(raw_seat)
    # API 的 seat_type：default=标准席位（GPT），usage_based=Usage-based（Codex）。
    gpt_label = "GPT席位" if seat == "default" else ""
    codex_label = "Codex席位" if seat == "usage_based" else ""
    return {
        "gpt_seat": gpt_label,
        "codex_seat": codex_label,
        "member_id": str(item.get("id") or item.get("account_user_id") or ""),
        "raw_seat_type": seat or raw_seat.lower(),
    }


def fetch_candidate_seats(workspace_db_id: int, emails: list[str]) -> dict[str, dict[str, str]]:
    session, master = create_workspace_http_session(workspace_db_id)
    wid, token = master.get("workspace_id") or "", master.get("access_token") or ""
    out = {}
    for email in {str(e).lower() for e in emails}:
        response = _workspace_admin_request(
            workspace_db_id,
            session,
            "get",
            f"{BASE}/backend-api/accounts/{wid}/users",
            params={"offset": 0, "limit": 25, "query": email},
            headers=_headers(token, wid),
            timeout=30,
        )
        data = _json(response)
        items = data.get("items", []) if isinstance(data, dict) else []
        item = next((x for x in items if str(x.get("email") or "").lower() == email), {})
        out[email] = _candidate_seat_snapshot(item)
    return out


def fetch_candidate_seats_bulk(
    workspace_db_id: int,
    emails: list[str],
    *,
    page_size: int = 100,
    request_interval: float = WORKSPACE_ADMIN_REQUEST_INTERVAL_SECONDS,
    max_429_retries: int = 3,
) -> dict[str, dict[str, str]]:
    """分页拉取空间成员，再在本地匹配邮箱。

    成员同步使用这个入口，避免对每个候选邮箱分别请求一次 `/users`。
    单成员查询和席位切换后的复查仍使用 `fetch_candidate_seats`。
    """
    wanted = {
        str(email or "").strip().lower()
        for email in emails
        if str(email or "").strip()
    }
    if not wanted:
        return {}

    session, master = create_workspace_http_session(workspace_db_id)
    wid = str(master.get("workspace_id") or "").strip()
    token = str(master.get("access_token") or "").strip()
    if not wid or not token:
        raise RuntimeError("母号缺少 Workspace ID 或 Access Token")

    limit = max(1, min(100, int(page_size or 100)))
    offset = 0
    total: int | None = None
    pages = 0
    out: dict[str, dict[str, str]] = {}
    headers = _headers(token, wid)

    while total is None or offset < total:
        response = _workspace_admin_request(
            workspace_db_id,
            session,
            "get",
            f"{BASE}/backend-api/accounts/{wid}/users",
            request_interval=request_interval,
            max_429_retries=max_429_retries,
            params={"offset": offset, "limit": limit, "query": ""},
            headers=headers,
            timeout=30,
        )

        data = _json(response)
        pages += 1
        items = data.get("items", []) if isinstance(data, dict) else []
        if total is None:
            try:
                total = max(0, int(data.get("total") or 0))
            except (TypeError, ValueError):
                total = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip().lower()
            if email in wanted:
                out[email] = _candidate_seat_snapshot(item)

        if wanted.issubset(out):
            break
        received = len(items)
        if received <= 0:
            break
        offset += received
        if total and offset >= total:
            break
        if received < limit:
            break
        if request_interval > 0:
            time.sleep(float(request_interval))

    logger.info(
        "成员列表分页匹配完成 workspace_db_id=%s requested=%s matched=%s pages=%s total=%s",
        workspace_db_id,
        len(wanted),
        len(out),
        pages,
        total or 0,
    )
    return out

def update_member_seat_type(workspace_db_id: int, member_id: str, seat_type: str) -> dict:
    if seat_type not in {"default", "usage_based", "prolite"}: raise ValueError("席位类型只能是 default、usage_based 或 prolite")
    session, master = create_workspace_http_session(workspace_db_id)
    wid, token = master.get("workspace_id") or "", master.get("access_token") or ""
    response = _workspace_admin_request(
        workspace_db_id,
        session,
        "post",
        f"{BASE}/backend-api/accounts/{wid}/users/{member_id}/seat/update",
        headers={**_headers(token, wid), "Referer": f"{BASE}/admin/members"},
        json={
            "operation": "switch",
            "seat_type": seat_type,
            "flow_id": str(uuid.uuid4()),
            "mutation_attempt_id": str(uuid.uuid4()),
        },
        timeout=30,
    )
    data = _json(response)
    if not isinstance(data, dict) or data.get("success") is not True: raise RuntimeError(f"成员席位切换失败: {data}")
    return data


def _canonical_candidate_seat_type(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"usage_based", "usagebased", "codex"} or "codex席位" in normalized:
        return "usage_based"
    if normalized in {"default", "standard", "standard_seat", "gpt"} or "gpt席位" in normalized or "标准席位" in normalized:
        return "default"
    if normalized in {"prolite", "pro_lite", "advanced", "advanced_seat", "premium", "premium_seat", "pro", "高级", "高级席位"}:
        return "prolite"
    return normalized


def _refresh_candidate_seat_snapshot(workspace_db_id: int, email: str) -> dict:
    current = fetch_candidate_seats(workspace_db_id, [email]).get(str(email).strip().lower(), {})
    if current:
        db.update_workspace_candidate_seats(
            workspace_db_id,
            email,
            current.get("codex_seat", ""),
            current.get("gpt_seat", ""),
        )
        db.update_workspace_candidate_member(
            workspace_db_id,
            email,
            current.get("member_id", ""),
            current.get("raw_seat_type", ""),
        )
    return current


def resolve_candidate_seat_type(
    workspace_db_id: int,
    email: str,
    *,
    payload: dict | None = None,
) -> str:
    """解析候选人当前席位类型。

    优先级：
    1. 本地候选席位缓存；
    2. 本次登录/凭证结果里携带的席位字段；
    3. 远端 Team 管理接口查询；

    返回 canonical seat_type：default / usage_based / prolite / ""。
    """
    email = str(email or "").strip().lower()
    if not email:
        return ""
    sources = []
    if isinstance(payload, dict):
        sources.extend([
            payload.get("seat_type"),
            payload.get("seatType"),
            payload.get("seat_label"),
            payload.get("seatLabel"),
            payload.get("raw_seat_type"),
            payload.get("rawSeatType"),
            payload.get("workspace_seat_type"),
            payload.get("workspaceSeatType"),
            payload.get("account_seat_type"),
            payload.get("accountSeatType"),
        ])
    for value in sources:
        seat = _canonical_candidate_seat_type(value)
        if seat in {"default", "usage_based", "prolite"}:
            return seat
    row = db.get_workspace_candidate(workspace_db_id, email) or {}
    # 已经拿到当前空间凭证时，说明该成员已经成功进入空间；
    # 这时不要再回母号查 members/users，直接使用本地缓存即可。
    if db.list_workspace_credentials_by_emails(workspace_db_id, [email]):
        for value in (row.get("seat_type"), row.get("gpt_seat"), row.get("codex_seat")):
            seat = _canonical_candidate_seat_type(value)
            if seat in {"default", "usage_based", "prolite"}:
                return seat
        return ""
    refreshed = _refresh_candidate_seat_snapshot(workspace_db_id, email)
    seat = _canonical_candidate_seat_type(refreshed.get("raw_seat_type") or refreshed.get("seat_type"))
    if seat in {"default", "usage_based", "prolite"}:
        return seat
    for value in (row.get("seat_type"), row.get("gpt_seat"), row.get("codex_seat")):
        seat = _canonical_candidate_seat_type(value)
        if seat in {"default", "usage_based", "prolite"}:
            return seat
    return ""


def _ensure_candidate_usage_based(workspace_db_id: int, email: str, row: dict | None = None, retries: int = 3) -> dict:
    """把候选人席位收敛到 usage_based，并在最后复查一次当前席位。"""
    email = str(email or "").strip().lower()
    if not email:
        return {}
    row = dict(row or {})
    seat_type = _canonical_candidate_seat_type(row.get("seat_type"))
    if not row:
        row = db.get_workspace_candidate(workspace_db_id, email) or {}
        seat_type = _canonical_candidate_seat_type(row.get("seat_type"))
    if seat_type == "usage_based":
        return _refresh_candidate_seat_snapshot(workspace_db_id, email)
    info = _refresh_candidate_seat_snapshot(workspace_db_id, email)
    seat_type = _canonical_candidate_seat_type(info.get("raw_seat_type") or row.get("seat_type"))
    member_id = str(info.get("member_id") or row.get("member_id") or "").strip()
    if seat_type == "usage_based":
        return info
    if not member_id:
        logger.warning("垃圾箱席位复查失败：未找到 member_id workspace_db_id=%s email=%s", workspace_db_id, email)
        return info
    last_error = None
    for attempt in range(max(1, int(retries))):
        try:
            update_member_seat_type(workspace_db_id, member_id, "usage_based")
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "垃圾箱席位切换失败 workspace_db_id=%s email=%s attempt=%s/%s",
                workspace_db_id, email, attempt + 1, retries,
                exc_info=True,
            )
            time.sleep(1 + attempt)
    refreshed = {}
    for attempt in range(3):
        refreshed = _refresh_candidate_seat_snapshot(workspace_db_id, email)
        if _canonical_candidate_seat_type(refreshed.get("raw_seat_type") or refreshed.get("seat_type")) == "usage_based":
            return refreshed
        if attempt < 2:
            time.sleep(5)

    # 查询始终未确认 usage_based 时，不能把本地缓存“猜成” Codex。
    # 垃圾箱调用方会据此判定失败并保留原候选状态，等待下一轮重试。
    logger.warning(
        "垃圾箱席位复查未确认 usage_based workspace_db_id=%s email=%s final_seat=%s last_error=%s",
        workspace_db_id,
        email,
        refreshed.get("raw_seat_type") or refreshed.get("seat_type") or "unknown",
        last_error,
    )
    return refreshed or {"member_id": member_id}


def trash_workspace_candidate(workspace_db_id: int, email: str, reason: str = "", retries: int = 3) -> dict:
    """将单个候选人移入垃圾箱，并确保席位切到 usage_based。"""
    email = str(email or "").strip().lower()
    if not email:
        return {"ok": False, "error": "email 不能为空"}
    if not db.get_workspace_master(workspace_db_id):
        return {"ok": False, "pending_seat": False, "error": "母号不存在，不执行入箱"}
    row = db.get_workspace_candidate(workspace_db_id, email) or {}
    try:
        seat = _ensure_candidate_usage_based(workspace_db_id, email, row=row, retries=retries)
    except Exception as exc:
        logger.warning(
            "候选人移入垃圾箱前席位切换失败 workspace_db_id=%s email=%s error=%s",
            workspace_db_id, email, str(exc)[:240],
        )
        return {"ok": False, "pending_seat": True, "error": str(exc)}
    final_seat = _canonical_candidate_seat_type(
        (seat or {}).get("raw_seat_type")
        or (seat or {}).get("seat_type")
        or (seat or {}).get("seat_label")
        or (seat or {}).get("codex_seat")
        or (seat or {}).get("gpt_seat")
    )
    if final_seat != "usage_based":
        logger.warning(
            "候选人移入垃圾箱失败：未确认 usage_based workspace_db_id=%s email=%s seat=%s",
            workspace_db_id,
            email,
            (seat or {}).get("raw_seat_type") or (seat or {}).get("seat_type") or "unknown",
        )
        return {
            "ok": False,
            "pending_seat": True,
            "error": "席位未确认切换为 Codex（usage based），不会移入垃圾箱",
            "seat": seat,
        }
    # 只有远端复查确认 usage_based 后，才允许改变垃圾箱状态。
    updated = db.update_workspace_candidate_trash(
        workspace_db_id,
        email,
        status="trashed",
        reason=reason or "manual",
        due_at=0,
    )
    if not updated:
        return {
            "ok": False,
            "pending_seat": False,
            "error": "候选关系不存在或母号已删除，不执行入箱",
            "seat": seat,
        }
    return {"ok": True, "seat": seat}


def trash_workspace_candidates_by_email(
    email: str,
    reason: str = "",
    retries: int = 3,
    *,
    respect_invalid_settings: bool = False,
) -> dict:
    """将该邮箱在所有母号空间中的候选关系都移入垃圾箱。"""
    key = str(email or "").strip().lower()
    if not key:
        return {"ok": False, "error": "email 不能为空"}
    rows = db.list_workspace_candidates_by_email(key)
    results = []
    for row in rows:
        workspace_db_id = int(row.get("workspace_master_id") or 0)
        if not workspace_db_id:
            continue
        if respect_invalid_settings and not db.get_workspace_settings(workspace_db_id).get(
            "trash_invalid_enabled", True,
        ):
            results.append({"workspace_db_id": workspace_db_id, "ok": True, "skipped": True})
            continue
        try:
            result = trash_workspace_candidate(
                workspace_db_id,
                key,
                reason=reason,
                retries=retries,
            )
            results.append({
                "workspace_db_id": workspace_db_id,
                "ok": bool(result.get("ok")),
                "seat": result.get("seat", {}),
                "pending_seat": bool(result.get("pending_seat")),
                "error": result.get("error", ""),
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("垃圾箱处理失败 workspace_db_id=%s email=%s", workspace_db_id, key)
            results.append({"workspace_db_id": workspace_db_id, "ok": False, "error": str(exc)})
    return {
        "ok": not any(not item.get("ok") for item in results),
        "results": results,
        "trashed": sum(1 for item in results if item.get("ok") and not item.get("skipped")),
        "skipped": sum(1 for item in results if item.get("skipped")),
        "failed": sum(1 for item in results if not item.get("ok")),
    }


def approve_candidate_request(workspace_db_id: int, email: str, seat_type: str = "default") -> dict:
    """母号接受指定候选人的待处理申请。"""
    session, master = create_workspace_http_session(workspace_db_id)
    workspace_id, token = master.get("workspace_id") or "", master.get("access_token") or ""
    headers = _headers(token, workspace_id)
    listed = _json(_workspace_admin_request(
        workspace_db_id,
        session,
        "get",
        f"{BASE}/backend-api/accounts/{workspace_id}/invites",
        params={"include_pending": "true", "include_requests": "true", "offset": 0, "limit": 100, "query": email},
        headers=headers,
        timeout=30,
    ))
    items = listed.get("items", []) if isinstance(listed, dict) else []
    target = email.lower(); invite_id = ""
    def walk(value):
        nonlocal invite_id
        if invite_id: return
        if isinstance(value, dict):
            vals = [value.get("email"), value.get("email_address"), value.get("recipient_email"), ((value.get("user") or {}).get("email") if isinstance(value.get("user"), dict) else "")]
            if any(str(v or "").strip().lower() == target for v in vals):
                invite_id = str(value.get("id") or value.get("invite_id") or value.get("inviteId") or value.get("request_id") or "")
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(items)
    if not invite_id: raise RuntimeError("未找到候选人的待处理申请")
    return _json(_workspace_admin_request(
        workspace_db_id,
        session,
        "patch",
        f"{BASE}/backend-api/accounts/{workspace_id}/invites/{invite_id}",
        headers=headers,
        json={"role": "standard-user", "seat_type": seat_type, "accept_request": True},
        timeout=30,
    ))


def request_join(workspace_db_id: int, candidate: dict, proxy: str, seat_type: str = "default") -> dict:
    token = candidate.get("access_token") or ""
    if not token: raise RuntimeError("子号缺少 Access Token")
    from . import db
    master = db.get_workspace_master(workspace_db_id)
    if not master or not master.get("workspace_id"): raise RuntimeError("母号缺少 Workspace ID")
    candidate_account = (_payload(token).get("https://api.openai.com/auth") or {}).get("chatgpt_account_id") or ""
    email = candidate.get("email") or ""
    if seat_type not in {"default", "usage_based", "prolite"}:
        raise ValueError("席位类型只能是标准席位、Usage-based 或 ProLite")
    logger.info("子号申请加入开始 workspace_db_id=%s workspace_id=%s email=%s candidate_account_id=%s seat_type=%s proxy_configured=%s", workspace_db_id, master['workspace_id'], email, candidate_account, seat_type, bool(str(proxy or '').strip()))
    try:
        proxy_value = str(proxy or "").strip()
        if not proxy_value:
            raise ValueError("全局代理池为空，子号申请加入无法租取代理")
        if not re.fullmatch(r"(?:(?:socks5h?|socks4|https?)://)?\S+:\d+", proxy_value, re.I):
            raise ValueError("全局代理池中的代理格式错误，应为 [协议://][user:pass@]host:port")
        session = create_http_session(proxy=proxy_value)
        # 当前上游申请加入接口要求空请求体；seat_type 不能随申请请求发送，
        # 否则会返回 422（服务端仅接受其内部默认席位类型）。席位类型由母号邀请/审批阶段设置。
        response = session.post(
            f"{BASE}/backend-api/accounts/{master['workspace_id']}/invites/request",
            headers=_headers(token, candidate_account), timeout=30,
        )
    except Exception:
        logger.exception("子号申请加入请求异常 workspace_db_id=%s workspace_id=%s email=%s", workspace_db_id, master['workspace_id'], email)
        raise
    logger.info("子号申请加入响应 workspace_db_id=%s workspace_id=%s email=%s status=%s", workspace_db_id, master['workspace_id'], email, response.status_code)
    return _json(response)


def sync_seat_info(workspace_db_id: int) -> dict:
    session, master = create_workspace_http_session(workspace_db_id)
    workspace_id, token = master.get("workspace_id") or "", master.get("access_token") or ""
    if not workspace_id or not token: raise RuntimeError("母号缺少 Workspace ID 或 Access Token")
    headers = _headers(token, workspace_id)
    usage = _json(_workspace_admin_request(
        workspace_db_id,
        session,
        "get",
        f"{BASE}/backend-api/subscriptions",
        params={"account_id": workspace_id},
        headers=headers,
        timeout=30,
    ))
    in_use = usage.get("seats_in_use")
    entitled = usage.get("seats_entitled")
    result = {
        "seats_in_use": in_use,
        "seats_entitled": entitled,
        "seats_default": None,
        "seats_default_entitled": None,
        "seats_usage_based": None,
        "seats_prolite": None,
        "seats_prolite_entitled": None,
        "seat_cost": "",
        "renewal_date": "",
    }
    # 新版 subscriptions 响应按席位类型返回订阅容量。default 与
    # prolite 都属于已购订阅席位，但必须分别保存，不能只使用总数
    # seats_entitled（例如 HAR 中分别购买 4+4，合计为 8）。
    seat_capacity = usage.get("seat_capacity") if isinstance(usage, dict) else None
    if isinstance(seat_capacity, list):
        paid_by_type = {}
        for item in seat_capacity:
            if not isinstance(item, dict):
                continue
            seat_type = _canonical_candidate_seat_type(item.get("type"))
            if seat_type not in {"default", "prolite"}:
                continue
            try:
                paid_by_type[seat_type] = int(item.get("paid") or 0)
            except (TypeError, ValueError):
                paid_by_type[seat_type] = 0
        result["seats_default_entitled"] = paid_by_type.get("default", 0)
        result["seats_prolite_entitled"] = paid_by_type.get("prolite", 0)
        if entitled is None:
            result["seats_entitled"] = sum(paid_by_type.values())
    counts = _json(_workspace_admin_request(
        workspace_db_id,
        session,
        "get",
        f"{BASE}/backend-api/accounts/{workspace_id}/users/seat_type_counts",
        headers=headers,
        timeout=30,
    ))
    seat_counts = counts.get("seat_type_counts") if isinstance(counts, dict) else None
    if isinstance(seat_counts, dict):
        result["seats_default"] = int(seat_counts.get("default") or 0)
        result["seats_usage_based"] = int(seat_counts.get("usage_based") or 0)
        result["seats_prolite"] = int(
            seat_counts.get("prolite")
            or seat_counts.get("pro_lite")
            or seat_counts.get("advanced")
            or seat_counts.get("advanced_seat")
            or seat_counts.get("premium")
            or seat_counts.get("premium_seat")
            or 0
        )
        logger.info("席位分类同步 workspace_db_id=%s workspace_id=%s default=%s usage_based=%s prolite=%s automation=%s", workspace_db_id, workspace_id, result["seats_default"], result["seats_usage_based"], result["seats_prolite"], seat_counts.get("automation", 0))
    if entitled is not None:
        preview = _json(_workspace_admin_request(
            workspace_db_id,
            session,
            "get",
            f"{BASE}/backend-api/subscriptions/update/preview",
            params={"account_id": workspace_id, "updated_seats": int(entitled) + 1},
            headers=headers,
            timeout=30,
        ))
        amount = preview.get("amount_due") or {}
        if amount.get("amount") is not None:
            result["seat_cost"] = f"{int(amount['amount']) / 100:.2f} {str(amount.get('currency') or '').upper()}"
        result["renewal_date"] = preview.get("renewal_date") or ""
    return result
