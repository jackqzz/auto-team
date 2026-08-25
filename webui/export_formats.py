"""批量导出格式注册表。

**以后要加导出格式，只改这一个文件**：往 `FORMATS` 里加一条就行。
后端路由、前端下拉框都是照着这张表自动长出来的，一行都不用动。

两种 mode：
  - `text`     一行一条记录，前端弹窗预览 + 复制 + 下载（`render` 逐行）
  - `download` 整份文档，前端拿到直接下载、不弹预览（`render_all` 返回 bytes）

约定（主人定的）：
- **不跳行**。勾了几个号就出几行，字段为空就留空，
  分隔符照样保留（`邮箱----`），方便主人自己在文本里对齐、补齐。
- 行序 = 「注册结果」表格里的顺序（created_at 倒序），好核对。

CPA / SUB2API 手动导出只做文件格式转换，不联网刷新 token。注册结果里保存的是
Codex token 时可以直接导入；如果某条只有 ChatGPT Web token，需先用“仅登录”补取 RT。
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from . import exporter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportFormat:
    id: str                                       # 前端 command 用的唯一标识
    label: str                                    # 下拉菜单里显示的名字
    filename: str                                 # 下载的文件名
    mode: str = "text"                            # "text" | "download"
    mime: str = "text/plain; charset=utf-8"
    render: Optional[Callable[[dict], str]] = None          # mode=text：一行记录 -> 一行文本
    render_all: Optional[Callable[[list], bytes]] = None    # mode=download：整批 -> 文件字节
    filename_for: Optional[Callable[[list], str]] = None    # 按本批内容动态生成下载文件名
    mime_for: Optional[Callable[[list], str]] = None        # 动态文件类型（如单 JSON / 多 ZIP）
    note: str = ""                                # 下拉菜单里的灰色小字说明


def _s(row: dict, key: str) -> str:
    """取字段并转成干净字符串（None / 非 str 都兜住）。"""
    v = row.get(key)
    if v is None:
        return ""
    return str(v).strip()


def _jwt_parts(row: dict) -> tuple[dict, dict, dict]:
    """返回 access payload/auth 与 id auth；坏 token 按空对象处理。"""
    access_payload = exporter._decode_jwt_payload(_s(row, "access_token"))
    access_auth = exporter._get_auth(access_payload)
    id_auth = exporter._get_auth(exporter._decode_jwt_payload(_s(row, "id_token")))
    return access_payload, access_auth, id_auth


def _first_org_id(id_auth: dict, access_auth: dict) -> str:
    org_id = str(id_auth.get("organization_id") or "").strip()
    for auth in (id_auth, access_auth):
        if org_id:
            break
        for org in auth.get("organizations") or []:
            if isinstance(org, dict) and str(org.get("id") or "").strip():
                org_id = str(org["id"]).strip()
                break
    return org_id or str(
        access_auth.get("organization_id") or access_auth.get("poid") or ""
    ).strip()


def _safe_filename_part(value: str, fallback: str = "unknown") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or "").strip())
    return cleaned or fallback


def _cpa_data(row: dict) -> dict:
    """生成与 codex-<hash>-<email>-<plan>.json 示例一致的 CPA 内容。"""
    data = exporter.build_cpa_token_json(row)
    # CPA 自身导出的文件包含 disabled；上传构造器为了兼容旧 API 没有该字段。
    data["disabled"] = False
    # 保持示例的字段顺序，方便肉眼 diff（JSON 消费方并不依赖顺序）。
    return {
        "access_token": data.get("access_token", ""),
        "account_id": data.get("account_id", ""),
        "disabled": False,
        "email": data.get("email", ""),
        "expired": data.get("expired", ""),
        "id_token": data.get("id_token", ""),
        "last_refresh": data.get("last_refresh", ""),
        "refresh_token": data.get("refresh_token", ""),
        "type": "codex",
    }


def _cpa_entry_name(row: dict, data: Optional[dict] = None) -> str:
    data = data or _cpa_data(row)
    _, access_auth, _ = _jwt_parts(row)
    account_id = str(data.get("account_id") or "").strip()
    digest_source = account_id or str(data.get("email") or "unknown")
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:8]
    plan = str(access_auth.get("chatgpt_plan_type") or "free").strip().lower() or "free"
    return (
        f"codex-{digest}-{_safe_filename_part(data.get('email', ''))}-"
        f"{_safe_filename_part(plan, 'free')}.json"
    )


def _render_cpa(rows: list) -> bytes:
    entries = [(row, _cpa_data(row)) for row in rows]
    if len(entries) == 1:
        return json.dumps(
            entries[0][1], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row, data in entries:
            archive.writestr(
                _cpa_entry_name(row, data),
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            )
    return output.getvalue()


def _cpa_filename(rows: list) -> str:
    if len(rows) == 1:
        return _cpa_entry_name(rows[0])
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"cpa-accounts-{stamp}.zip"


def _mailbox_info(row: dict) -> tuple[dict, str]:
    email = _s(row, "email")
    kind = _s(row, "pool_kind") or _s(row, "mail_kind")
    relay_url = _s(row, "relay_url")
    mail_password = _s(row, "mail_password")
    client_id = _s(row, "mail_client_id")
    mail_rt = _s(row, "mail_refresh_token")
    pickup = relay_url or mail_password
    if relay_url and kind == "icloud_relay" and mail_password:
        # 通用 OTP 的 password 列现在保存 OpenAI 登录密码；导出时保留三段
        # 导入格式，确保重新导入后仍可参与“补齐2FA”。
        source_line = f"{email}----{mail_password}----{relay_url}"
    elif relay_url:
        source_line = f"{email}----{relay_url}"
    else:
        source_line = f"{email}----{mail_password}----{client_id}----{mail_rt}"
    mailbox = {
        "bind_email": email,
        "primary_email": email,
        "password": mail_password,
        "client_id": client_id,
        "refresh_token": mail_rt,
        "pickup_password": pickup,
        "source_line": source_line,
    }
    if relay_url:
        mailbox["relay_url"] = relay_url
    if kind:
        mailbox["provider"] = kind
    return mailbox, pickup


def _sub2_account(row: dict) -> dict:
    email = _s(row, "email")
    access_payload, access_auth, _id_auth = _jwt_parts(row)
    profile = exporter._get_profile(access_payload)
    account_email = email or str(profile.get("email") or "").strip()
    expires_at = access_payload.get("exp")
    if not isinstance(expires_at, int):
        expires_at = 0
    account_id = str(
        access_auth.get("chatgpt_account_id") or access_auth.get("account_id") or ""
    ).strip()
    user_id = str(
        access_auth.get("chatgpt_user_id")
        or access_auth.get("user_id")
        or access_payload.get("sub")
        or ""
    ).strip()
    return {
        "name": account_email,
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": _s(row, "access_token"),
            "email": account_email,
            "password": _s(row, "password"),
            "totp_secret": _s(row, "totp_secret"),
            "expires_at": expires_at,
            "refresh_token": _s(row, "refresh_token"),
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "client_id": str(
                access_payload.get("client_id") or exporter.CODEX_CLIENT_ID
            ).strip(),
            "id_token": _s(row, "id_token"),
            "plan_type": str(access_auth.get("chatgpt_plan_type") or "free").strip() or "free",
        },
        "extra": {
            "openai_long_context_billing_enabled": False,
            "openai_oauth_responses_websockets_v2_enabled": False,
            "openai_oauth_responses_websockets_v2_mode": "off",
            "privacy_mode": "training_off",
            "source": "workspace_oauth",
            "workspace_id": account_id,
            "email": account_email,
        },
        "group_ids": [4],
        "priority": 1,
        "concurrency": 10,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


def _render_sub2(rows: list) -> bytes:
    accounts = [_sub2_account(row) for row in rows]
    document = {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "workspace_id": accounts[0]["credentials"].get("chatgpt_account_id", "") if accounts else "",
        "proxies": [],
        "accounts": accounts,
    }
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def _sub2_filename(rows: list) -> str:
    return f"sub2api-accounts-remaining-{len(rows)}.json"


# ──────────────────────── 注册表 ────────────────────────


FORMATS: list[ExportFormat] = [
    ExportFormat(
        id="at",
        label="access_token",
        filename="AT.txt",
        render=lambda r: _s(r, "access_token"),
    ),
    ExportFormat(
        id="email_pw",
        label="邮箱----密码",
        filename="账号密码.txt",
        render=lambda r: f'{_s(r, "email")}----{_s(r, "password")}',
    ),
    # 2FA secret 只在绑定那一刻下发一次、服务端取不回，丢了这个号就永久锁死，
    # 所以必须有能把它带出去的导出格式。没绑 2FA 的号照约定留空、分隔符保留。
    ExportFormat(
        id="email_pw_2fa",
        label="邮箱----密码----2FA",
        filename="账号密码2FA.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "password")}----{_s(r, "totp_secret")}'
        ),
        note="secret 仅下发一次，取不回，务必留存",
    ),
    # 比上面那条多一段中转取件链接。
    # ⚠️ relay_url 不在 registered 表里，是 db.list_registered_full /
    #    list_registered_by_emails 从号池表（outlook_accounts）LEFT JOIN 带出来的。
    #    所以：① 只有 icloud_relay 这类「一号一条取件链接」的号有值；
    #          ② 号池那行被删掉了就是空 —— 照约定留空、分隔符保留，不跳行。
    #    链接里嵌着 token，等于这个邮箱的收件权限，导出来的文件请当密码保管。
    ExportFormat(
        id="email_pw_2fa_relay",
        label="邮箱----密码----2FA----取件url",
        filename="账号密码2FA取件url.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "password")}----'
            f'{_s(r, "totp_secret")}----{_s(r, "relay_url")}'
        ),
        note="取件链接含 token，等同收件权限，妥善保管",
    ),
    ExportFormat(
        id="cpa",
        label="CPA 凭证 JSON",
        filename="cpa-accounts.zip",
        mode="download",
        mime="application/json",
        render_all=_render_cpa,
        filename_for=_cpa_filename,
        mime_for=lambda rows: "application/json" if len(rows) == 1 else "application/zip",
        note="单账号为 JSON，多账号打包 ZIP",
    ),
    ExportFormat(
        id="sub2api",
        label="Sub2API 账号 JSON",
        filename="sub2api-account.json",
        mode="download",
        mime="application/json",
        render_all=_render_sub2,
        filename_for=_sub2_filename,
        note="兼容 Sub2API 批量导入",
    ),
]

_BY_ID = {f.id: f for f in FORMATS}


def list_formats() -> list[dict]:
    """给前端的精简清单（不含 render 函数）。"""
    return [
        {
            "id": f.id,
            "label": f.label,
            "filename": f.filename,
            "mode": f.mode,
            "mime": f.mime,
            "note": f.note,
        }
        for f in FORMATS
    ]


def get_format(fmt_id: str) -> Optional[ExportFormat]:
    return _BY_ID.get((fmt_id or "").strip())


def render_text(rows: list, fmt: "ExportFormat | str") -> str:
    """mode=text：一行一条记录。

    单条渲染炸了不整体失败 —— 那一行留空，其余照常导出。
    """
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"未知导出格式: {fmt}")
    if not f.render:
        raise RuntimeError(f"格式 {f.id} 不是文本格式")

    lines = []
    for r in rows or []:
        try:
            lines.append(f.render(r))
        except Exception:
            lines.append("")
    return "\n".join(lines)


def render_bytes(rows: list, fmt: "ExportFormat | str") -> bytes:
    """mode=download：整份文件字节。"""
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"未知导出格式: {fmt}")
    if not f.render_all:
        raise RuntimeError(f"格式 {f.id} 不是下载格式")
    return f.render_all(rows or [])


# 兼容旧调用名
def render(rows: list, fmt: "ExportFormat | str") -> str:
    return render_text(rows, fmt)
