"""Team 空间候选人的真实邀请/申请及只读席位同步。"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

from http_client import create_http_session
from .workspace_client import create_workspace_http_session
from . import db

BASE = "https://chatgpt.com"
WORKSPACE_ADMIN_REQUEST_INTERVAL_SECONDS = 1.0
WORKSPACE_ADMIN_MAX_429_RETRIES = 3

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


class UpstreamHttpError(RuntimeError):
    def __init__(self, status_code: int, detail: object):
        self.status_code = int(status_code)
        super().__init__(f"上游 HTTP {self.status_code}: {detail}")

def fetch_candidate_quota(workspace_db_id: int, email: str, *, proxy: str) -> dict:
    """查询候选人的 Codex 额度，且必须使用本次从全局池租取的代理。

    额度请求携带的是候选人的 Team Access Token，不属于母号管理请求，禁止
    复用 ``workspace_masters.proxy_url``。调用方必须显式传入代理，避免配置
    缺失时悄悄回退到母号出口或直连。
    """
    proxy_value = str(proxy or "").strip()
    if not proxy_value:
        raise ValueError("全局代理池为空，候选额度查询无法租取代理")
    master = db.get_workspace_master(workspace_db_id)
    if not master:
        raise RuntimeError("母号不存在")
    session = create_http_session(proxy=proxy_value)
    rows = db.list_workspace_credentials_by_emails(workspace_db_id, [email])
    if not rows: raise RuntimeError("候选人尚未获取当前空间凭证")
    cred = rows[0]; token = cred.get("access_token") or ""; wid = master.get("workspace_id") or ""
    last = None
    for attempt in range(3):
        try:
            response = session.get(f"{BASE}/backend-api/wham/usage", headers={**_headers(token, wid), "ChatGPT-Account-Id": wid, "User-Agent": "codex-cli"}, timeout=30)
            if response.status_code >= 500 and attempt < 2:
                time.sleep(1 + attempt); continue
            break
        except Exception as exc:
            last = exc
            if attempt >= 2: raise RuntimeError(f"额度查询网络错误（已重试2次）：{exc}") from exc
            time.sleep(1 + attempt)
    if response.status_code >= 300:
        code = int(response.status_code)
        db.update_workspace_quota(workspace_db_id, email, {"error_code": code, "updated_at": time.time()})
        db.update_workspace_candidate_status(workspace_db_id, email, f"quota_error_{code}")
        if code == 401: raise QuotaUnauthorized("额度查询失败 HTTP 401")
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
    **kwargs,
):
    """串行并节流同一母号的管理 API 请求，并对 429 做共享退避。"""
    key = int(workspace_db_id)
    interval = max(0.0, float(request_interval or 0.0))
    retries = max(0, int(max_429_retries or 0))
    request = getattr(session, str(method).strip().lower())

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
            finally:
                with _workspace_admin_state_lock:
                    _workspace_admin_last_completed[key] = time.monotonic()

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
        "Referer": f"{BASE}/admin/{account_id}",
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
    if seat_type not in {"default", "usage_based"}:
        raise ValueError("席位类型只能是标准席位或 Usage-based")
    logger.info("母号批量邀请开始 workspace_db_id=%s workspace_id=%s seat_type=%s count=%s emails=%s", workspace_db_id, workspace_id, seat_type, len(emails), emails[:20])
    try:
        response = _workspace_admin_request(
            workspace_db_id,
            session,
            "post",
            f"{BASE}/backend-api/accounts/{workspace_id}/invites",
            headers=_headers(token, workspace_id),
            json={"email_addresses": emails, "role": "standard-user", "seat_type": seat_type, "resend_emails": True},
            timeout=30,
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
    """通过 invites/users 确认状态；同一母号的查询严格串行并节流。"""
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
    seat = str(item.get("seat_type") or item.get("seatType") or item.get("seat") or "").lower()
    # API 的 seat_type：default=标准席位（GPT），usage_based=Usage-based（Codex）。
    gpt_label = "GPT席位" if seat in {"default", "standard", "standard-seat"} else ""
    codex_label = "Codex席位" if seat in {"usage_based", "usage-based", "usagebased"} else ""
    return {
        "gpt_seat": gpt_label,
        "codex_seat": codex_label,
        "member_id": str(item.get("id") or item.get("account_user_id") or ""),
        "raw_seat_type": seat,
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
    if seat_type not in {"default", "usage_based"}: raise ValueError("席位类型只能是 default 或 usage_based")
    session, master = create_workspace_http_session(workspace_db_id)
    wid, token = master.get("workspace_id") or "", master.get("access_token") or ""
    response = _workspace_admin_request(
        workspace_db_id,
        session,
        "patch",
        f"{BASE}/backend-api/accounts/{wid}/users/{member_id}",
        headers={**_headers(token, wid), "Referer": f"{BASE}/admin/members"},
        json={"seat_type": seat_type},
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

    返回 canonical seat_type：default / usage_based / ""。
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
        if seat in {"default", "usage_based"}:
            return seat
    row = db.get_workspace_candidate(workspace_db_id, email) or {}
    # 已经拿到当前空间凭证时，说明该成员已经成功进入空间；
    # 这时不要再回母号查 members/users，直接使用本地缓存即可。
    if db.list_workspace_credentials_by_emails(workspace_db_id, [email]):
        for value in (row.get("seat_type"), row.get("gpt_seat"), row.get("codex_seat")):
            seat = _canonical_candidate_seat_type(value)
            if seat in {"default", "usage_based"}:
                return seat
        return ""
    refreshed = _refresh_candidate_seat_snapshot(workspace_db_id, email)
    seat = _canonical_candidate_seat_type(refreshed.get("raw_seat_type") or refreshed.get("seat_type"))
    if seat in {"default", "usage_based"}:
        return seat
    for value in (row.get("seat_type"), row.get("gpt_seat"), row.get("codex_seat")):
        seat = _canonical_candidate_seat_type(value)
        if seat in {"default", "usage_based"}:
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

    # 上游可能已经成功切换，但查询有延迟；此时先把本地快照纠正为
    # usage_based，避免垃圾箱列表继续显示旧席位。
    logger.warning(
        "垃圾箱席位复查延迟，强制刷新本地快照 workspace_db_id=%s email=%s final_seat=%s last_error=%s",
        workspace_db_id,
        email,
        refreshed.get("raw_seat_type") or refreshed.get("seat_type") or "unknown",
        last_error,
    )
    db.update_workspace_candidate_seats(workspace_db_id, email, "Codex席位", "")
    db.update_workspace_candidate_member(workspace_db_id, email, member_id, "usage_based")
    return _refresh_candidate_seat_snapshot(workspace_db_id, email) or {
        "member_id": member_id,
        "raw_seat_type": "usage_based",
        "codex_seat": "Codex席位",
        "gpt_seat": "",
    }


def trash_workspace_candidate(workspace_db_id: int, email: str, reason: str = "", retries: int = 3) -> dict:
    """将单个候选人移入垃圾箱，并确保席位切到 usage_based。"""
    email = str(email or "").strip().lower()
    if not email:
        return {"ok": False, "error": "email 不能为空"}
    row = db.get_workspace_candidate(workspace_db_id, email) or {}
    db.update_workspace_candidate_trash(
        workspace_db_id,
        email,
        status="trashed",
        reason=reason or "manual",
        due_at=0,
    )
    seat = _ensure_candidate_usage_based(workspace_db_id, email, row=row, retries=retries)
    return {"ok": True, "seat": seat}


def trash_workspace_candidates_by_email(email: str, reason: str = "", retries: int = 3) -> dict:
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
        try:
            results.append({
                "workspace_db_id": workspace_db_id,
                "ok": True,
                "seat": trash_workspace_candidate(workspace_db_id, key, reason=reason, retries=retries).get("seat", {}),
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("垃圾箱处理失败 workspace_db_id=%s email=%s", workspace_db_id, key)
            results.append({"workspace_db_id": workspace_db_id, "ok": False, "error": str(exc)})
    return {
        "ok": not any(not item.get("ok") for item in results),
        "results": results,
        "trashed": sum(1 for item in results if item.get("ok")),
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
    if seat_type not in {"default", "usage_based"}:
        raise ValueError("席位类型只能是标准席位或 Usage-based")
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
    result = {"seats_in_use": in_use, "seats_entitled": entitled, "seats_default": None, "seats_usage_based": None, "seat_cost": "", "renewal_date": ""}
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
        logger.info("席位分类同步 workspace_db_id=%s workspace_id=%s default=%s usage_based=%s automation=%s", workspace_db_id, workspace_id, result["seats_default"], result["seats_usage_based"], seat_counts.get("automation", 0))
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
