"""SQLite 号池 + 注册结果存储。

表结构：
  outlook_accounts: 接码号池（多种邮箱混放，kind 列区分 + 状态机）
  registered:       注册成功结果（凭证 JSON）

关于 outlook_accounts 这个表名：
    它现在装的不止 outlook（还有 gmail / icloud / qq ...），名字已经不准，
    但改表名要动迁移和一堆 SQL，收益只是好看一点，风险不值。
    真正区分类型的是 kind 列。

凭证字段用「并集列」而不是 extra_json：
    outlook/gmail 用 password+client_id+refresh_token，
    icloud 这类中转只用 relay_url，各自把不用的列留空。
    几种邮箱的规模下，并集列比 JSON 好 —— 能建索引、能加约束、
    SQL 里直接看得见。加新邮箱时如果要新字段，就再 ALTER 加一列。
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import secrets
import sys
import threading
import time
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = Path(__file__).resolve().parent / "webui.db"

_lock = threading.Lock()  # SQLite 写入串行化


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outlook_accounts (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            client_id       TEXT,
            refresh_token   TEXT,
            relay_url       TEXT,       -- 中转取码 URL（icloud 类用，其余留空）
            kind            TEXT NOT NULL DEFAULT 'outlook',
                            -- 邮箱类型，对应 mail_providers 注册表的 kind
            group_name      TEXT NOT NULL DEFAULT '',
                            -- 用户分组；空字符串表示“未分组”
            status          TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed
            imported_at     REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_outlook_status ON outlook_accounts(status);
        -- idx_outlook_kind 不在这里建：老库此刻还没有 kind 列，
        -- 建索引会当场报错。放到下面补完列之后再建。

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT
        );

        CREATE TABLE IF NOT EXISTS account_groups (
            name            TEXT PRIMARY KEY,
            created_at      REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS registered (
            email           TEXT PRIMARY KEY,
            group_name      TEXT NOT NULL DEFAULT '',
            mail_kind       TEXT NOT NULL DEFAULT '',
            password        TEXT,
            access_token    TEXT,
            session_token   TEXT,
            refresh_token   TEXT,
            id_token        TEXT,
            device_id       TEXT,
            csrf_token      TEXT,
            cookie_header   TEXT,
            totp_secret     TEXT,
            totp_factor_id  TEXT,
            account_status  TEXT NOT NULL DEFAULT 'active',
            extra_json      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS workspace_masters (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            account         TEXT NOT NULL COLLATE NOCASE UNIQUE,
            email           TEXT NOT NULL DEFAULT '',
            workspace_id    TEXT NOT NULL DEFAULT '',
            access_token    TEXT NOT NULL DEFAULT '',
            seats_in_use    INTEGER,
            seats_entitled  INTEGER,
            seats_default   INTEGER,
            seats_usage_based INTEGER,
            seat_cost       TEXT NOT NULL DEFAULT '',
            renewal_date    TEXT NOT NULL DEFAULT '',
            session_token   TEXT NOT NULL UNIQUE,
            proxy_url       TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'imported',
            imported_at     REAL NOT NULL,
            updated_at      REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_workspace_masters_updated
            ON workspace_masters(updated_at DESC);

        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- network / account / unknown
        );
    """)
    con.commit()

    workspace_cols = {
        row[1] for row in con.execute("PRAGMA table_info(workspace_masters)").fetchall()
    }
    if "proxy_url" not in workspace_cols:
        con.execute(
            "ALTER TABLE workspace_masters ADD COLUMN proxy_url TEXT NOT NULL DEFAULT ''"
        )
        con.commit()
    for col, definition in (
        ("email", "TEXT NOT NULL DEFAULT ''"),
        ("workspace_id", "TEXT NOT NULL DEFAULT ''"),
        ("access_token", "TEXT NOT NULL DEFAULT ''"),
        ("seats_in_use", "INTEGER"),
        ("seats_entitled", "INTEGER"),
        ("seats_default", "INTEGER"),
        ("seats_usage_based", "INTEGER"),
        ("seat_cost", "TEXT NOT NULL DEFAULT ''"),
        ("renewal_date", "TEXT NOT NULL DEFAULT ''"),
        ("settings_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in workspace_cols:
            con.execute(f"ALTER TABLE workspace_masters ADD COLUMN {col} {definition}")
            con.commit()
    con.execute("""CREATE TABLE IF NOT EXISTS workspace_candidates (
        workspace_master_id INTEGER NOT NULL,
        email TEXT NOT NULL COLLATE NOCASE,
        status TEXT NOT NULL DEFAULT 'candidate',
        trash_status TEXT NOT NULL DEFAULT 'active',
        trash_due_at REAL,
        trash_reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (workspace_master_id, email),
        FOREIGN KEY (workspace_master_id) REFERENCES workspace_masters(id) ON DELETE CASCADE
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_workspace_candidates_email ON workspace_candidates(email)")
    cand_cols = {r[1] for r in con.execute("PRAGMA table_info(workspace_candidates)").fetchall()}
    for col, definition in (
        ("codex_seat", "TEXT NOT NULL DEFAULT ''"),
        ("gpt_seat", "TEXT NOT NULL DEFAULT ''"),
        ("seat_type", "TEXT NOT NULL DEFAULT ''"),
        ("member_id", "TEXT NOT NULL DEFAULT ''"),
        ("trash_status", "TEXT NOT NULL DEFAULT 'active'"),
        ("trash_due_at", "REAL"),
        ("trash_reason", "TEXT NOT NULL DEFAULT ''"),
        ("tag_status", "TEXT NOT NULL DEFAULT 'active'"),
    ):
        if col not in cand_cols:
            con.execute(f"ALTER TABLE workspace_candidates ADD COLUMN {col} {definition}")
    if "workspace_join_status" not in cand_cols:
        con.execute("ALTER TABLE workspace_candidates ADD COLUMN workspace_join_status TEXT NOT NULL DEFAULT 'not_invited'")
        # 旧版本把空间加入状态和账号/额度状态混在 status 中；可识别的
        # 加入状态迁移到独立字段，永久失效和额度错误则不污染加入状态。
        con.execute("""UPDATE workspace_candidates
            SET workspace_join_status=CASE
                WHEN status IN ('not_invited','pending_invite','pending_request','joined','join_requested','approved') THEN status
                ELSE 'not_invited'
            END""")
        con.commit()
    con.execute("""CREATE TABLE IF NOT EXISTS workspace_credentials (
        workspace_master_id INTEGER NOT NULL,
        email TEXT NOT NULL COLLATE NOCASE,
        access_token TEXT NOT NULL DEFAULT '', session_token TEXT NOT NULL DEFAULT '',
        refresh_token TEXT NOT NULL DEFAULT '', id_token TEXT NOT NULL DEFAULT '',
        quota_json TEXT,
        device_id TEXT NOT NULL DEFAULT '', csrf_token TEXT NOT NULL DEFAULT '',
        cookie_header TEXT NOT NULL DEFAULT '', extra_json TEXT, created_at REAL NOT NULL,
        PRIMARY KEY (workspace_master_id, email),
        FOREIGN KEY (workspace_master_id) REFERENCES workspace_masters(id) ON DELETE CASCADE
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_workspace_credentials_email ON workspace_credentials(email)")
    if "quota_json" not in {r[1] for r in con.execute("PRAGMA table_info(workspace_credentials)").fetchall()}:
        con.execute("ALTER TABLE workspace_credentials ADD COLUMN quota_json TEXT")
    # 兼容早期版本：空间登录曾暂时写入 registered。根据 AT 中的 workspace id
    # 回填到独立表，避免历史上已获取的 Team 凭证无法导出。
    import base64 as _b64
    for master in con.execute("SELECT id, workspace_id FROM workspace_masters WHERE workspace_id<>''").fetchall():
        for row in con.execute("SELECT * FROM registered WHERE access_token<>''").fetchall():
            try:
                part = str(row["access_token"]).split(".")[1]
                payload = json.loads(_b64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                account_id = str((payload.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id") or "")
            except Exception:
                account_id = ""
            if account_id == str(master["workspace_id"]):
                con.execute("""INSERT OR IGNORE INTO workspace_credentials
                    (workspace_master_id,email,access_token,session_token,refresh_token,id_token,device_id,csrf_token,cookie_header,extra_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (master["id"], row["email"], row["access_token"], row["session_token"], row["refresh_token"], row["id_token"], row["device_id"], row["csrf_token"], row["cookie_header"], row["extra_json"], row["created_at"]))
    con.commit()

    _repair_workspace_candidate_join_statuses(con)
    con.commit()


    # 老 DB migrate：error_category 在后期才加，对已建表补列
    cur = con.execute("PRAGMA table_info(runs)")
    cols = {r[1] for r in cur.fetchall()}
    if "error_category" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        con.commit()

    # 老 DB migrate：号池多邮箱混放（kind / relay_url 在后期才加）
    # 存量行全部是 outlook 时代导进去的，DEFAULT 'outlook' 正好把它们
    # 归位，不需要额外 UPDATE。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(outlook_accounts)")
    acc_cols = {r[1] for r in cur.fetchall()}
    if "kind" not in acc_cols:
        con.execute(
            "ALTER TABLE outlook_accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'outlook'"
        )
        con.commit()
    if "relay_url" not in acc_cols:
        con.execute("ALTER TABLE outlook_accounts ADD COLUMN relay_url TEXT")
        con.commit()
    if "group_name" not in acc_cols:
        con.execute("ALTER TABLE outlook_accounts ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
        con.commit()
    # 索引建在补列之后，否则老库上 CREATE INDEX 会因为没有 kind 列而失败
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outlook_kind ON outlook_accounts(kind, status)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outlook_group ON outlook_accounts(group_name, kind, status)"
    )
    # 兼容第一版分组功能已经写入的账号：将现有非空分组登记到独立分组表。
    con.execute(
        "INSERT OR IGNORE INTO account_groups(name, created_at) "
        "SELECT DISTINCT group_name, ? FROM outlook_accounts WHERE group_name <> ''",
        (time.time(),),
    )
    con.commit()

    # 老 DB migrate：registered 的 2FA 两列（totp_secret / totp_factor_id）后期才加。
    # secret 一次性下发、服务端取不回，务必单独补列持久化。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(registered)")
    reg_cols = {r[1] for r in cur.fetchall()}
    if "totp_secret" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_secret TEXT")
        con.commit()
    if "totp_factor_id" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_factor_id TEXT")
        con.commit()
    if "account_status" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'")
        con.commit()
    if "group_name" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
        con.commit()
    # 兼容分组功能开发期的中间库：列已存在但尚未从邮箱池回填。
    con.execute(
        "UPDATE registered SET group_name=COALESCE(("
        "SELECT group_name FROM outlook_accounts WHERE outlook_accounts.email=registered.email"
        "), '') WHERE group_name='' AND EXISTS ("
        "SELECT 1 FROM outlook_accounts WHERE outlook_accounts.email=registered.email "
        "AND outlook_accounts.group_name<>'')"
    )
    con.commit()
    if "mail_kind" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN mail_kind TEXT NOT NULL DEFAULT ''")
        con.commit()
    con.execute(
        "UPDATE registered SET mail_kind=COALESCE(("
        "SELECT kind FROM outlook_accounts WHERE outlook_accounts.email=registered.email"
        "), (SELECT value FROM settings WHERE key='mail_source'), 'outlook') "
        "WHERE mail_kind=''"
    )
    con.commit()
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_registered_group ON registered(group_name, created_at)"
    )
    con.commit()


# ──────────────────────── Team 工作空间母号 ────────────────────────


def normalize_workspace_proxy(proxy: str) -> str:
    """校验母号专属代理；保留原协议，裸 host:port 由 HTTP 客户端按 http 使用。"""
    value = str(proxy or "").strip()
    if not value:
        raise ValueError("母号必须设置专属代理")
    if not re.fullmatch(r"(?:(?:socks5h?|socks4|https?)://)?\S+:\d+", value, re.I):
        raise ValueError("代理格式错误，应为 [协议://][user:pass@]host:port")
    return value


def _mask_proxy(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return "未设置"
    # 保留协议、用户名与出口，隐藏密码；没有鉴权信息时原样显示。
    return re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1***\2", value, count=1)


def _workspace_import_rows(text: str, default_proxy: str = "") -> list[dict]:
    """解析母号：支持 account----session----proxy、纯 session 与 JSON。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("请输入母号 Session")
    candidates = None
    if raw[:1] in ("[", "{"):
        try:
            payload = json.loads(raw)
            candidates = payload if isinstance(payload, list) else [payload]
        except json.JSONDecodeError:
            candidates = None
    if candidates is None:
        candidates = [line.strip() for line in raw.splitlines() if line.strip()]

    rows, errors = [], []
    for idx, item in enumerate(candidates, 1):
        account = session = proxy = ""
        metadata = {}
        if isinstance(item, dict):
            raw_account = item.get("email") or item.get("name") or ""
            if not raw_account and isinstance(item.get("account"), str):
                raw_account = item.get("account")
            account = str(raw_account).strip()
            session = str(item.get("session_token") or item.get("session") or item.get("sessionToken") or "").strip()
            proxy = str(item.get("proxy") or item.get("proxy_url") or "").strip()
            # tmp.session.json/官方导出的 Session 文档：Session Token、邮箱和
            # workspace(account) 信息都在同一个 JSON 中。
            if not session and item.get("sessionToken"):
                session = str(item["sessionToken"]).strip()
            metadata = item
        elif isinstance(item, str):
            line = item.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    raw_account = obj.get("email") or obj.get("name") or ""
                    if not raw_account and isinstance(obj.get("account"), str):
                        raw_account = obj.get("account")
                    account = str(raw_account).strip()
                    session = str(obj.get("session_token") or obj.get("session") or obj.get("sessionToken") or "").strip()
                    proxy = str(obj.get("proxy") or obj.get("proxy_url") or "").strip()
                    metadata = obj
                except Exception:
                    errors.append(f"第 {idx} 行 JSON 格式错误")
                    continue
            elif "----" in line:
                parts = [part.strip() for part in line.split("----", 2)]
                account = parts[0]
                session = parts[1] if len(parts) > 1 else ""
                proxy = parts[2] if len(parts) > 2 else ""
            else:
                session = line
        else:
            errors.append(f"第 {idx} 项不是字符串或对象")
            continue
        if not session:
            errors.append(f"第 {idx} 行缺少 session")
            continue
        # 也允许直接粘贴 tmp.session.json；优先使用文档中的真实字段。
        if metadata.get("accessToken") and metadata.get("account"):
            account_obj = metadata.get("account") or {}
            user_obj = metadata.get("user") or {}
            account = account or str(user_obj.get("email") or "").strip()
            if isinstance(account_obj, dict):
                workspace_id = str(account_obj.get("id") or account_obj.get("workspace_id") or "").strip()
            else:
                workspace_id = ""
            access_token = str(metadata.get("accessToken") or "").strip()
        else:
            workspace_id = str(metadata.get("workspace_id") or metadata.get("workspaceId") or "").strip()
            access_token = str(metadata.get("access_token") or metadata.get("accessToken") or "").strip()
            if not account:
                account = str((metadata.get("user") or {}).get("email") or "").strip()
        if len(session) < 16:
            errors.append(f"第 {idx} 行 session 过短")
            continue
        try:
            proxy = normalize_workspace_proxy(proxy or default_proxy)
        except ValueError as e:
            errors.append(f"第 {idx} 行: {e}")
            continue
        if not account:
            digest = hashlib.sha256(session.encode("utf-8")).hexdigest()[:12]
            account = f"母号-{digest}"
        if "@" in account:
            account = account.lower()
        rows.append({"account": account[:255], "email": account[:255],
                     "workspace_id": workspace_id[:255], "access_token": access_token,
                     "session_token": session, "proxy_url": proxy})
    if errors:
        raise ValueError("；".join(errors))
    if not rows:
        raise ValueError("没有可导入的母号 Session")
    return rows


def import_workspace_sessions(text: str, proxy: str = "") -> dict:
    rows = _workspace_import_rows(text, default_proxy=proxy)
    inserted = updated = skipped = 0
    now = time.time()
    with _lock:
        con = _conn()
        for item in rows:
            old = con.execute(
                "SELECT id, account, email, workspace_id, access_token, session_token, proxy_url FROM workspace_masters "
                "WHERE account=? OR session_token=? ORDER BY account=? DESC LIMIT 1",
                (item["account"], item["session_token"], item["account"]),
            ).fetchone()
            if old is None:
                con.execute(
                    "INSERT INTO workspace_masters"
                    "(account, email, workspace_id, access_token, session_token, proxy_url, status, imported_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'imported', ?, ?)",
                    (item["account"], item["email"], item["workspace_id"], item["access_token"], item["session_token"], item["proxy_url"], now, now),
                )
                inserted += 1
            elif (
                old["account"] == item["account"]
                and old["email"] == item["email"]
                and old["workspace_id"] == item["workspace_id"]
                and old["access_token"] == item["access_token"]
                and old["session_token"] == item["session_token"]
                and old["proxy_url"] == item["proxy_url"]
            ):
                skipped += 1
            else:
                con.execute(
                    "UPDATE workspace_masters SET account=?, email=?, workspace_id=?, access_token=?, session_token=?, proxy_url=?, "
                    "status='imported', updated_at=? WHERE id=?",
                    (item["account"], item["email"], item["workspace_id"], item["access_token"], item["session_token"], item["proxy_url"], now, old["id"]),
                )
                updated += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def count_workspace_masters() -> int:
    return _conn().execute("SELECT COUNT(*) FROM workspace_masters").fetchone()[0]


def list_workspace_masters(limit: int = 20, offset: int = 0) -> list[dict]:
    rows = _conn().execute(
        "SELECT id, account, email, workspace_id, seats_in_use, seats_entitled, seats_default, seats_usage_based, seat_cost, renewal_date, status, length(session_token) AS session_len, "
        "substr(session_token, 1, 8) AS session_head, "
        "substr(session_token, -6) AS session_tail, proxy_url, imported_at, updated_at "
        "FROM workspace_masters ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (max(1, min(int(limit), 200)), max(0, int(offset))),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["session_preview"] = f'{item.pop("session_head")}…{item.pop("session_tail")}'
        item["proxy_preview"] = _mask_proxy(item.pop("proxy_url", ""))
        out.append(item)
    return out


def get_workspace_master(workspace_id: int) -> Optional[dict]:
    row = _conn().execute("SELECT * FROM workspace_masters WHERE id=?", (int(workspace_id),)).fetchone()
    return dict(row) if row else None


def get_workspace_master_by_external_id(external_id: str) -> Optional[dict]:
    row = _conn().execute("SELECT * FROM workspace_masters WHERE workspace_id=? LIMIT 1", (str(external_id or ""),)).fetchone()
    return dict(row) if row else None


def list_workspace_master_ids_by_external_id(external_id: str) -> list[int]:
    rows = _conn().execute(
        "SELECT id FROM workspace_masters WHERE workspace_id=? ORDER BY id ASC",
        (str(external_id or ""),),
    ).fetchall()
    return [int(row[0]) for row in rows]


def list_workspace_master_ids_for_master(workspace_master_id: int) -> list[int]:
    master = get_workspace_master(workspace_master_id)
    if not master:
        return [int(workspace_master_id)]
    external_id = str(master.get("workspace_id") or "").strip()
    ids = list_workspace_master_ids_by_external_id(external_id) if external_id else []
    if not ids:
        return [int(workspace_master_id)]
    return ids

_WORKSPACE_SETTINGS_DEFAULTS = {
    "interval_minutes": 30,
    "relogin_on_401": False,
    "proxy_pool": "",
    "auto_push": False,
    "concurrency": 1,
    "otp_timeout": 180,
    "account_retry_count": 1,
    "cool_down_seconds": 0,
    "quota_enabled": False,
    "trash_enabled": True,
    "trash_invalid_enabled": True,
    "trash_zero_delay_minutes": 60,
    "seat_protect_enabled": False,
    "seat_protect_threshold": 8,
    "seat_protect_refresh_time": "00:00",
    "seat_protect_used_count": 0,
    "seat_protect_window_key": "",
    "auto_standard_seat_enabled": False,
}

_CST = timezone(timedelta(hours=8))


def _normalize_hhmm(value: object, default: str = "00:00") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return default
    hour = max(0, min(23, int(m.group(1))))
    minute = max(0, min(59, int(m.group(2))))
    return f"{hour:02d}:{minute:02d}"


def _workspace_seat_protect_window_key(now_ts: float | None, refresh_time: object) -> str:
    refresh = _normalize_hhmm(refresh_time)
    hour_s, minute_s = refresh.split(":", 1)
    hour = int(hour_s)
    minute = int(minute_s)
    now = datetime.fromtimestamp(float(now_ts or time.time()), tz=_CST)
    current_minutes = now.hour * 60 + now.minute
    refresh_minutes = hour * 60 + minute
    anchor = now if current_minutes >= refresh_minutes else now - timedelta(days=1)
    return anchor.strftime("%Y-%m-%d")


def get_workspace_settings(workspace_id: int) -> dict:
    """Return settings for exactly one workspace master.

    Defaults are applied in memory so old rows (whose settings_json is ``{}``)
    behave consistently after a restart.  ``workspace_id`` is deliberately not
    part of the returned settings; it is the database row key, not a setting.
    """
    row = _conn().execute(
        "SELECT settings_json FROM workspace_masters WHERE id=?", (int(workspace_id),)
    ).fetchone()
    try:
        raw = json.loads(row["settings_json"] or "{}") if row else {}
        values = raw if isinstance(raw, dict) else {}
    except Exception:
        values = {}
    values = {k: v for k, v in values.items() if k != "workspace_id"}
    if values.get("seat_protect_enabled"):
        refresh_time = _normalize_hhmm(values.get("seat_protect_refresh_time") or "00:00")
        current_key = _workspace_seat_protect_window_key(time.time(), refresh_time)
        stored_key = str(values.get("seat_protect_window_key") or "").strip()
        if stored_key != current_key:
            values["seat_protect_window_key"] = current_key
            values["seat_protect_used_count"] = 0
            update_workspace_settings(int(workspace_id), {
                "seat_protect_window_key": current_key,
                "seat_protect_used_count": 0,
            })
    result = dict(_WORKSPACE_SETTINGS_DEFAULTS)
    result.update(values)
    return result


def update_workspace_settings(workspace_id: int, values: dict) -> None:
    """Atomically merge settings belonging to one workspace master.

    The previous read-then-write happened outside the write lock, so two
    autosaves could read the same old JSON and one update would erase the
    other.  Reading and writing under the same lock keeps each workspace's
    settings isolated and durable.
    """
    clean = {
        str(key): value
        for key, value in (values or {}).items()
        if str(key) != "workspace_id"
    }
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT settings_json FROM workspace_masters WHERE id=?",
            (int(workspace_id),),
        ).fetchone()
        if not row:
            return
        try:
            raw = json.loads(row["settings_json"] or "{}")
            current = raw if isinstance(raw, dict) else {}
        except Exception:
            current = {}
        current.pop("workspace_id", None)
        current.update(clean)
        con.execute(
            "UPDATE workspace_masters SET settings_json=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), time.time(), int(workspace_id)),
        )
        con.commit()


def reserve_workspace_seat_protect_quota(workspace_id: int, amount: int = 1) -> dict:
    amount = max(1, int(amount or 1))
    with _lock:
        con = _conn()
        row = con.execute("SELECT settings_json FROM workspace_masters WHERE id=?", (int(workspace_id),)).fetchone()
        if not row:
            return {"ok": False, "allowed": False, "enabled": False, "error": "母号不存在"}
        try:
            raw = json.loads(row["settings_json"] or "{}")
            settings = raw if isinstance(raw, dict) else {}
        except Exception:
            settings = {}
        enabled = bool(settings.get("seat_protect_enabled", False))
        threshold = int(settings.get("seat_protect_threshold") or 8)
        refresh_time = _normalize_hhmm(settings.get("seat_protect_refresh_time") or "00:00")
        window_key = _workspace_seat_protect_window_key(time.time(), refresh_time)
        stored_window = str(settings.get("seat_protect_window_key") or "").strip()
        used = int(settings.get("seat_protect_used_count") or 0)
        if stored_window != window_key:
            used = 0
        allowed = (not enabled) or (used + amount <= threshold)
        result = {
            "ok": True,
            "enabled": enabled,
            "allowed": allowed,
            "used_count": used,
            "threshold": threshold,
            "refresh_time": refresh_time,
            "window_key": window_key,
        }
        if not allowed:
            return result
        if enabled:
            settings["seat_protect_enabled"] = enabled
            settings["seat_protect_threshold"] = threshold
            settings["seat_protect_refresh_time"] = refresh_time
            settings["seat_protect_window_key"] = window_key
            settings["seat_protect_used_count"] = used + amount
            con.execute(
                "UPDATE workspace_masters SET settings_json=?, updated_at=? WHERE id=?",
                (json.dumps(settings, ensure_ascii=False), time.time(), int(workspace_id)),
            )
            con.commit()
            result["used_count"] = used + amount
        return result


def release_workspace_seat_protect_quota(workspace_id: int, amount: int = 1) -> bool:
    amount = max(1, int(amount or 1))
    with _lock:
        con = _conn()
        row = con.execute("SELECT settings_json FROM workspace_masters WHERE id=?", (int(workspace_id),)).fetchone()
        if not row:
            return False
        try:
            raw = json.loads(row["settings_json"] or "{}")
            settings = raw if isinstance(raw, dict) else {}
        except Exception:
            settings = {}
        if not bool(settings.get("seat_protect_enabled", False)):
            return False
        refresh_time = _normalize_hhmm(settings.get("seat_protect_refresh_time") or "00:00")
        window_key = _workspace_seat_protect_window_key(time.time(), refresh_time)
        stored_window = str(settings.get("seat_protect_window_key") or "").strip()
        used = int(settings.get("seat_protect_used_count") or 0)
        if stored_window != window_key:
            used = 0
        settings["seat_protect_window_key"] = window_key
        settings["seat_protect_used_count"] = max(0, used - amount)
        con.execute(
            "UPDATE workspace_masters SET settings_json=?, updated_at=? WHERE id=?",
            (json.dumps(settings, ensure_ascii=False), time.time(), int(workspace_id)),
        )
        con.commit()
        return True


def delete_workspace_master(workspace_id: int) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM workspace_masters WHERE id=?", (int(workspace_id),))
        con.commit()
        return rc.rowcount > 0


def update_workspace_proxy(workspace_id: int, proxy: str) -> bool:
    value = normalize_workspace_proxy(proxy)
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE workspace_masters SET proxy_url=?, updated_at=? WHERE id=?",
            (value, time.time(), int(workspace_id)),
        )
        con.commit()
        return rc.rowcount > 0


def delete_workspace_masters(ids: list[int]) -> int:
    cleaned = sorted({int(i) for i in (ids or []) if int(i) > 0})
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            f"DELETE FROM workspace_masters WHERE id IN ({','.join('?' * len(cleaned))})", cleaned
        )
        con.commit()
        return rc.rowcount


def list_workspace_candidates(workspace_master_id: int = 0) -> list[dict]:
    join_status_expr = _workspace_candidate_join_status_expr()
    sql = """SELECT c.workspace_master_id, c.email, c.status,
                     COALESCE(c.trash_status, 'active') AS trash_status,
                     COALESCE(c.trash_due_at, 0) AS trash_due_at,
                     COALESCE(c.trash_reason, '') AS trash_reason,
                     """ + join_status_expr + """ AS workspace_join_status,
                     c.created_at, c.updated_at,
                     r.password, r.access_token, r.session_token, r.refresh_token,
                     r.group_name, r.account_status
              FROM workspace_candidates c
              LEFT JOIN registered r ON r.email=c.email
              LEFT JOIN workspace_credentials wc ON wc.workspace_master_id=c.workspace_master_id AND wc.email=c.email"""
    args = []
    if int(workspace_master_id or 0):
        sql += " WHERE c.workspace_master_id=?"; args.append(int(workspace_master_id))
    sql += " ORDER BY c.updated_at DESC"
    return [dict(r) for r in _conn().execute(sql, args).fetchall()]


def _workspace_candidate_option_filters(
    workspace_master_id: int,
    account_status: str = "",
    join_status: str = "",
    credential_status: str = "",
    seat_type: str = "",
    trash_status: str = "",
    tag_status: str = "",
):
    join_status_expr = _workspace_candidate_join_status_expr()
    clauses = ["c.workspace_master_id=?"]
    args = [int(workspace_master_id)]
    if account_status:
        clauses.append("COALESCE(r.account_status, 'active')=?")
        args.append(str(account_status))
    if join_status:
        clauses.append(f"({join_status_expr})=?")
        args.append(str(join_status))
    if credential_status == "workspace_credential":
        clauses.append("(length(COALESCE(wc.access_token,''))>0 OR c.status='workspace_credential')")
        clauses.append("COALESCE(r.account_status, 'active') <> 'permanently_invalid'")
    elif credential_status == "personal_credential":
        clauses.append("length(COALESCE(wc.access_token,''))=0 AND length(COALESCE(r.access_token,''))>0")
        clauses.append("COALESCE(r.account_status, 'active') <> 'permanently_invalid'")
    elif credential_status == "none":
        clauses.append("length(COALESCE(wc.access_token,''))=0 AND length(COALESCE(r.access_token,''))=0")
        clauses.append("COALESCE(r.account_status, 'active') <> 'permanently_invalid'")
    elif credential_status == "unavailable":
        clauses.append("COALESCE(r.account_status, 'active')='permanently_invalid'")
    if seat_type == "none":
        clauses.append("COALESCE(c.seat_type,'')=''")
    elif seat_type:
        normalized = str(seat_type).strip().lower().replace("-", "_")
        if normalized == "default":
            clauses.append(
                "LOWER(COALESCE(c.seat_type,'')) IN ('default', 'gpt席位', '标准席位')"
            )
        elif normalized == "usage_based":
            clauses.append(
                "LOWER(COALESCE(c.seat_type,'')) IN ('usage_based', 'usagebased', 'usage-based', 'codex席位')"
            )
        else:
            clauses.append("c.seat_type=?")
            args.append(str(seat_type))
    if trash_status:
        normalized = str(trash_status).strip().lower()
        if normalized not in {"active", "scheduled", "trashed"}:
            raise ValueError("trash_status 只能是 active / scheduled / trashed")
        clauses.append("COALESCE(c.trash_status, 'active')=?")
        args.append(normalized)
    normalized_tag = str(tag_status or "").strip().lower()
    if normalized_tag:
        if normalized_tag not in {"active", "outbound"}:
            raise ValueError("tag_status 只能是 active / outbound")
        clauses.append("COALESCE(c.tag_status, 'active')=?")
        args.append(normalized_tag)
    else:
        clauses.append("COALESCE(c.tag_status, 'active')<>'outbound'")
    return " AND ".join(clauses), args


def list_workspace_candidate_options(
    workspace_master_id: int,
    limit: int | None = None,
    offset: int = 0,
    account_status: str = "",
    join_status: str = "",
    credential_status: str = "",
    seat_type: str = "",
    trash_status: str = "",
    tag_status: str = "",
) -> list[dict]:
    where, args = _workspace_candidate_option_filters(
        workspace_master_id, account_status, join_status, credential_status, seat_type, trash_status, tag_status,
    )
    join_status_expr = _workspace_candidate_join_status_expr()
    sql = """SELECT r.email, r.group_name, c.seat_type, c.member_id,
        c.gpt_seat,
        c.codex_seat,
        CASE WHEN COALESCE(c.codex_seat, '') <> '' THEN c.codex_seat
             WHEN COALESCE(c.gpt_seat, '') <> '' THEN c.gpt_seat
             ELSE c.seat_type END AS seat_label,
        c.status,
        COALESCE(c.trash_status, 'active') AS trash_status,
        COALESCE(c.trash_due_at, 0) AS trash_due_at,
        COALESCE(c.trash_reason, '') AS trash_reason,
        COALESCE(c.tag_status, 'active') AS tag_status,
        """ + join_status_expr + """ AS workspace_join_status,
        wc.quota_json,
        r.account_status,
        CASE WHEN COALESCE(r.account_status, 'active') <> 'permanently_invalid' AND length(r.access_token)>0 THEN 1 ELSE 0 END AS has_access_token,
        CASE WHEN COALESCE(r.account_status, 'active') <> 'permanently_invalid' AND length(COALESCE(wc.access_token,''))>0 THEN 1 ELSE 0 END AS has_workspace_access_token,
        CASE WHEN COALESCE(c.trash_status, 'active')='trashed' THEN 'trashed'
             WHEN COALESCE(c.trash_status, 'active')='scheduled' THEN 'trash_scheduled'
             WHEN COALESCE(r.account_status, 'active')='permanently_invalid' THEN 'unavailable'
             WHEN c.status='workspace_credential' THEN 'workspace_credential'
             WHEN length(COALESCE(wc.access_token,''))>0 THEN 'workspace_credential'
             WHEN length(COALESCE(r.access_token,''))>0 THEN 'personal_credential'
             ELSE 'none' END AS credential_status,
        CASE WHEN COALESCE(c.trash_status, 'active')='trashed' THEN 'trashed'
             WHEN COALESCE(c.trash_status, 'active')='scheduled' THEN 'trash_scheduled'
             WHEN COALESCE(r.account_status, 'active')='permanently_invalid' THEN 'unavailable'
             WHEN c.status LIKE 'quota_error_%' THEN c.status
             WHEN c.status='workspace_credential' THEN 'workspace_credential'
             WHEN length(COALESCE(wc.access_token,''))>0 THEN 'workspace_credential'
             ELSE CASE WHEN """ + join_status_expr + """ = 'joined' THEN 'joined' ELSE c.workspace_join_status END END AS display_status,
        1 AS assigned
        FROM registered r JOIN workspace_candidates c
          ON c.email=r.email
        LEFT JOIN workspace_credentials wc ON wc.email=r.email AND wc.workspace_master_id=?
        WHERE """ + where.replace("c.workspace_master_id=?", "c.workspace_master_id=?") + " ORDER BY r.created_at DESC"
    # workspace id 同时用于 JOIN 左表和过滤条件。
    query_args = [int(workspace_master_id), *args]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        query_args.extend([max(1, int(limit)), max(0, int(offset))])
    rows = _conn().execute(sql, query_args).fetchall()
    return [dict(r) for r in rows]


def count_workspace_candidate_options(
    workspace_master_id: int,
    account_status: str = "",
    join_status: str = "",
    credential_status: str = "",
    seat_type: str = "",
    trash_status: str = "",
    tag_status: str = "",
) -> int:
    where, args = _workspace_candidate_option_filters(
        workspace_master_id, account_status, join_status, credential_status, seat_type, trash_status, tag_status,
    )
    return int(_conn().execute("""SELECT COUNT(*) FROM registered r
        JOIN workspace_candidates c ON c.email=r.email
        LEFT JOIN workspace_credentials wc ON wc.email=r.email AND wc.workspace_master_id=?
        WHERE """ + where, [int(workspace_master_id), *args]).fetchone()[0])


def get_workspace_candidate(workspace_master_id: int, email: str) -> Optional[dict]:
    row = _conn().execute(
        "SELECT * FROM workspace_candidates WHERE workspace_master_id=? AND email=?",
        (int(workspace_master_id), str(email or "").strip().lower()),
    ).fetchone()
    return dict(row) if row else None


def update_workspace_candidate_join_statuses(
    workspace_master_id: int,
    emails: list[str],
    join_status: str,
) -> int:
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not cleaned:
        return 0
    normalized = str(join_status or "").strip()
    if normalized not in {"not_invited", "pending_invite", "joined"}:
        raise ValueError("邀请状态只能是 not_invited / pending_invite / joined")
    changed = 0
    with _lock:
        con = _conn()
        now = time.time()
        for email in cleaned:
            account = con.execute(
                "SELECT account_status FROM registered WHERE email=?",
                (email,),
            ).fetchone()
            legacy_status = normalized
            if account and str(account["account_status"] or "") == "permanently_invalid":
                legacy_status = "permanently_invalid"
            rc = con.execute(
                "UPDATE workspace_candidates SET status=?, workspace_join_status=?, updated_at=? "
                "WHERE workspace_master_id=? AND email=?",
                (legacy_status, normalized, now, int(workspace_master_id), email),
            )
            changed += rc.rowcount
        con.commit()
    return changed


def list_workspace_candidates_by_email(email: str) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM workspace_candidates WHERE email=? ORDER BY updated_at DESC",
        (str(email or "").strip().lower(),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_workspace_candidate_trash_due(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else float(now)
    rows = _conn().execute(
        """
        SELECT c.*, m.workspace_id AS workspace_external_id, m.access_token AS workspace_access_token
          FROM workspace_candidates c
          JOIN workspace_masters m ON m.id = c.workspace_master_id
         WHERE c.trash_status='scheduled'
           AND COALESCE(c.trash_due_at, 0) > 0
           AND c.trash_due_at <= ?
         ORDER BY c.trash_due_at ASC, c.updated_at ASC
        """,
        (now,),
    ).fetchall()
    return [dict(row) for row in rows]


def update_workspace_candidate_trash(
    workspace_master_id: int,
    email: str,
    *,
    status: str,
    due_at: float | None = None,
    reason: str = "",
) -> bool:
    status = str(status or "active").strip().lower()
    if status not in {"active", "scheduled", "trashed"}:
        raise ValueError("trash_status 只能是 active / scheduled / trashed")
    key = str(email or "").strip().lower()
    if not key:
        return False
    now = time.time()
    if status == "active":
        due_at = 0
        reason = ""
    elif status == "trashed":
        due_at = 0
    with _lock:
        con = _conn()
        if due_at is None:
            current_due = con.execute(
                "SELECT trash_due_at FROM workspace_candidates WHERE workspace_master_id=? AND email=?",
                (int(workspace_master_id), key),
            ).fetchone()
            due_value = float(current_due["trash_due_at"] or 0) if current_due else 0
        else:
            due_value = float(due_at or 0)
        rc = con.execute(
            """
            UPDATE workspace_candidates
               SET trash_status=?, trash_due_at=?, trash_reason=?, updated_at=?
             WHERE workspace_master_id=? AND email=?
            """,
            (status, due_value, str(reason or "")[:500], now, int(workspace_master_id), key),
        )
        con.commit()
        return rc.rowcount > 0


def update_workspace_candidates_trash_by_email(
    email: str,
    *,
    status: str,
    reason: str = "",
) -> int:
    key = str(email or "").strip().lower()
    if not key:
        return 0
    rows = list_workspace_candidates_by_email(key)
    changed = 0
    for row in rows:
        changed += int(
            update_workspace_candidate_trash(
                row["workspace_master_id"],
                key,
                status=status,
                reason=reason,
            )
        )
    return changed


def save_workspace_credential(workspace_master_id: int, d: dict) -> None:
    """保存指定 Team 空间的凭证，不覆盖 registered 中的 Personal/Free 凭证。"""
    email = str(d.get("email") or "").strip().lower()
    wid = int(workspace_master_id or 0)
    if not email or not wid:
        return
    known = {"email", "access_token", "session_token", "refresh_token", "id_token", "device_id", "csrf_token", "cookie_header"}
    extra = {k: v for k, v in d.items() if k not in known}
    now = time.time()
    master_ids = list_workspace_master_ids_for_master(wid)
    with _lock:
        con = _conn()
        for master_id in master_ids:
            con.execute("""INSERT OR REPLACE INTO workspace_credentials
                (workspace_master_id,email,access_token,session_token,refresh_token,id_token,device_id,csrf_token,cookie_header,extra_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (master_id, email, d.get("access_token", ""), d.get("session_token", ""), d.get("refresh_token", ""), d.get("id_token", ""), d.get("device_id", ""), d.get("csrf_token", ""), d.get("cookie_header", ""), json.dumps(extra, ensure_ascii=False) if extra else None, now))
            if str(d.get("access_token") or "").strip() or str(d.get("refresh_token") or "").strip():
                con.execute(
                    "INSERT OR IGNORE INTO workspace_candidates(workspace_master_id,email,status,workspace_join_status,created_at,updated_at) VALUES (?,?, 'workspace_credential', 'joined', ?, ?)",
                    (master_id, email, now, now),
                )
                con.execute(
                    "UPDATE workspace_candidates SET workspace_join_status='joined', status='workspace_credential', updated_at=? WHERE workspace_master_id=? AND email=?",
                    (now, master_id, email),
                )
        con.commit()


def list_workspace_credentials_by_emails(workspace_master_id: int, emails: list[str]) -> list[dict]:
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not cleaned:
        return []
    marks = ",".join("?" * len(cleaned))
    master_ids = list_workspace_master_ids_for_master(workspace_master_id)
    if not master_ids:
        master_ids = [int(workspace_master_id)]
    master_marks = ",".join("?" * len(master_ids))
    rows = _conn().execute(f"""SELECT COALESCE(r.email, wc.email) AS email,
        r.group_name, r.password, r.totp_secret, r.account_status,
        r.created_at, r.extra_json,
        wc.access_token AS workspace_access_token,
        wc.session_token AS workspace_session_token, wc.refresh_token AS workspace_refresh_token,
        wc.id_token AS workspace_id_token, wc.device_id AS workspace_device_id, wc.quota_json,
        wc.csrf_token AS workspace_csrf_token, wc.cookie_header AS workspace_cookie_header,
        wc.workspace_master_id AS workspace_master_id
        FROM workspace_credentials wc
        LEFT JOIN registered r ON r.email=wc.email
        WHERE wc.workspace_master_id IN ({master_marks}) AND wc.email IN ({marks})
        ORDER BY COALESCE(r.created_at, wc.created_at) DESC, wc.workspace_master_id ASC""", [*master_ids, *cleaned]).fetchall()
    out = []
    seen = set()
    for row in rows:
        item = dict(row)
        email = str(item.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        if item.get("extra_json"):
            try:
                item["extra"] = json.loads(item["extra_json"])
            except Exception:
                item["extra"] = {}
        else:
            item["extra"] = {}
        for field in ("access_token", "session_token", "refresh_token", "id_token", "device_id", "csrf_token", "cookie_header"):
            item[field] = item.get("workspace_" + field) or ""
        item.setdefault("mail_kind", "")
        item.setdefault("pool_kind", "")
        item.pop("extra_json", None)
        out.append(item)
    return out

def update_workspace_quota(workspace_master_id: int, email: str, quota: dict) -> None:
    key = str(email or "").strip().lower()
    wid = int(workspace_master_id or 0)
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE workspace_credentials SET quota_json=? WHERE workspace_master_id=? AND email=?",
            (json.dumps(quota, ensure_ascii=False), wid, key),
        )
        if quota and not quota.get("error_code"):
            row = con.execute(
                "SELECT c.workspace_join_status, c.status, r.account_status "
                "FROM workspace_candidates c LEFT JOIN registered r ON r.email=c.email "
                "WHERE c.workspace_master_id=? AND c.email=?",
                (wid, key),
            ).fetchone()
            if row and str(row["account_status"] or "") != "permanently_invalid":
                current = str(row["workspace_join_status"] or "").strip() or "not_invited"
                legacy = str(row["status"] or "").strip()
                if legacy.startswith("quota_error_"):
                    con.execute(
                        "UPDATE workspace_candidates SET status=?, updated_at=? WHERE workspace_master_id=? AND email=?",
                        (current, time.time(), wid, key),
                    )
        con.commit()


def assign_workspace_candidates(workspace_master_id: int, emails: list[str]) -> int:
    wid = int(workspace_master_id)
    if not get_workspace_master(wid):
        raise ValueError("母号不存在")
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not cleaned: return 0
    now = time.time(); added = 0
    with _lock:
        con = _conn()
        for email in cleaned:
            account = con.execute(
                "SELECT account_status FROM registered WHERE email=?", (email,)
            ).fetchone()
            if not account or account["account_status"] == "permanently_invalid":
                continue
            rc = con.execute("INSERT OR IGNORE INTO workspace_candidates(workspace_master_id,email,status,workspace_join_status,created_at,updated_at) VALUES (?,?, 'not_invited', 'not_invited', ?, ?)", (wid,email,now,now))
            added += rc.rowcount
        con.commit()
    return added


def remove_workspace_candidates(workspace_master_id: int, emails: list[str]) -> int:
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not cleaned: return 0
    with _lock:
        con = _conn(); marks = ','.join('?' * len(cleaned))
        rc = con.execute(f"DELETE FROM workspace_candidates WHERE workspace_master_id=? AND email IN ({marks})", [int(workspace_master_id), *cleaned]); con.commit(); return rc.rowcount


def update_workspace_candidate_tag_status(workspace_master_id: int, emails: list[str], tag_status: str) -> int:
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    normalized = str(tag_status or "").strip().lower()
    if normalized not in {"active", "outbound"}:
        raise ValueError("tag_status 只能是 active / outbound")
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        marks = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"UPDATE workspace_candidates SET tag_status=?, updated_at=? WHERE workspace_master_id=? AND email IN ({marks})",
            [normalized, time.time(), int(workspace_master_id), *cleaned],
        )
        con.commit()
        return rc.rowcount


def update_workspace_candidate_status(workspace_master_id: int, email: str, status: str) -> bool:
    with _lock:
        con = _conn()
        key = str(email).lower()
        account = con.execute("SELECT account_status FROM registered WHERE email=?", (key,)).fetchone()
        now = time.time()
        if status == "permanently_invalid":
            # 全局账号状态与空间加入状态相互独立；旧 status 列仅保留作兼容显示。
            rc = con.execute(
                "UPDATE workspace_candidates SET status='permanently_invalid', updated_at=? WHERE workspace_master_id=? AND email=?",
                (now, int(workspace_master_id), key),
            )
        elif str(status).startswith("quota_error_"):
            legacy_status = status if not (account and account["account_status"] == "permanently_invalid") else "permanently_invalid"
            if account and account["account_status"] == "permanently_invalid":
                rc = con.execute(
                    "UPDATE workspace_candidates SET status=?, updated_at=? WHERE workspace_master_id=? AND email=?",
                    (legacy_status, now, int(workspace_master_id), key),
                )
            else:
                rc = con.execute(
                    "UPDATE workspace_candidates SET status=?, updated_at=? WHERE workspace_master_id=? AND email=?",
                    (legacy_status, now, int(workspace_master_id), key),
                )
        else:
            # 账号已全局失效时不能覆盖 status，但仍然必须记录空间加入状态，
            # 这样已加入空间的失效成员仍可切换席位。
            legacy_status = status if not (account and account["account_status"] == "permanently_invalid") else "permanently_invalid"
            if account and account["account_status"] == "permanently_invalid":
                rc = con.execute(
                    "UPDATE workspace_candidates SET status=?, workspace_join_status=?, updated_at=? WHERE workspace_master_id=? AND email=?",
                    (legacy_status, status, now, int(workspace_master_id), key),
                )
            else:
                rc = con.execute(
                    "UPDATE workspace_candidates SET status=?, workspace_join_status=?, updated_at=? WHERE workspace_master_id=? AND email=?",
                    (legacy_status, status, now, int(workspace_master_id), key),
                )
        con.commit()
        return rc.rowcount > 0


def update_workspace_seat_info(workspace_master_id: int, **values) -> bool:
    allowed = {k: values[k] for k in ('seats_in_use','seats_entitled','seats_default','seats_usage_based','seat_cost','renewal_date') if k in values}
    if not allowed: return False
    clause = ', '.join(f'{k}=?' for k in allowed)
    with _lock:
        con = _conn(); rc = con.execute(f"UPDATE workspace_masters SET {clause}, updated_at=? WHERE id=?", [*allowed.values(), time.time(), int(workspace_master_id)]); con.commit(); return rc.rowcount > 0


# ──────────────────────── outlook 号池 ────────────────────────


def parse_lines(text: str, kind: str = "") -> list[dict]:
    """解析导入文本，委托给 mail_providers 注册表。

    kind 指定 → 用该 provider 的格式解析（推荐）
    kind 为空 → 按段数猜（段数唯一时才行，Outlook/Gmail 都是 4 段会猜不出）

    非法行抛 ImportValidationError（带行号和原因），**不再静默跳过**。
    以前这里是 `if len(parts) != 4: continue`，用户看到"导入成功"
    但号少了几个，完全没法排查。
    """
    from mail_providers import parse_import_text

    return parse_import_text(text or "", kind)


def import_accounts(
    text: str, kind: str = "", group_name: str | None = None,
) -> dict:
    """批量入库。已存在的 email 仅在凭证变化时更新。

    解析阶段全对才写：有一行非法就整批拒绝（抛 ImportValidationError），
    不会出现"写进去一半"对不上账的情况。
    """
    rows = parse_lines(text, kind)
    # None 表示旧调用方没有指定分组，此时重复导入不能意外移动已有账号。
    # 空字符串则是前端明确选择了“未分组”。
    target_group = _normalize_group_name(group_name) if group_name is not None else None
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        if target_group:
            con.execute(
                "INSERT OR IGNORE INTO account_groups(name, created_at) VALUES (?, ?)",
                (target_group, now),
            )
        for r in rows:
            row_kind = r.get("kind") or kind or "outlook"
            # 凭证并集：不同 provider 用不同子集，没有的留空字符串
            password = r.get("password", "") or ""
            client_id = r.get("client_id", "") or ""
            refresh = r.get("refresh_token", "") or ""
            relay = r.get("relay_url", "") or ""

            cur = con.execute(
                "SELECT refresh_token, relay_url, kind, group_name "
                "FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "relay_url, kind, group_name, status, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'available', ?)",
                    (
                        r["email"], password, client_id, refresh, relay, row_kind,
                        target_group or "", now,
                    ),
                )
                inserted += 1
                continue

            credentials_changed = (
                (existing["refresh_token"] or "") != refresh
                or (existing["relay_url"] or "") != relay
                or (existing["kind"] or "") != row_kind
            )
            group_changed = (
                target_group is not None
                and (existing["group_name"] or "") != target_group
            )
            if credentials_changed:
                # 凭证或类型变了 → 覆盖并重置为可用
                imported_group = (
                    target_group
                    if target_group is not None
                    else (existing["group_name"] or "")
                )
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "relay_url=?, kind=?, group_name=?, status='available', imported_at=?, "
                    "fail_reason=NULL "
                    "WHERE email=?",
                    (
                        refresh, password, client_id, relay, row_kind, imported_group,
                        now, r["email"],
                    ),
                )
                updated += 1
            elif group_changed:
                # 仅改分组时保留账号当前状态，避免把 done/failed 意外重置为 available。
                con.execute(
                    "UPDATE outlook_accounts SET group_name=? WHERE email=?",
                    (target_group, r["email"]),
                )
                updated += 1
            else:
                skipped += 1

            if group_changed:
                # 已注册结果与邮箱池使用同一分组，两个列表不能各显示一套归属。
                con.execute(
                    "UPDATE registered SET group_name=? WHERE email=?",
                    (target_group, r["email"]),
                )
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def _normalize_group_name(group_name: str | None) -> str:
    """空串是未分组；分组名不允许保留给前端协议的特殊值。"""
    name = (group_name or "").strip()
    if name == "__all__":
        raise ValueError("__all__ 是保留分组名")
    if len(name) > 64:
        raise ValueError("分组名称最长 64 个字符")
    return name


def count_accounts(status: str = "", kind: str = "", group_name: str | None = None) -> int:
    con = _conn()
    sql = "SELECT COUNT(*) FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if group_name is not None and group_name != "__all__":
        where.append("group_name=?")
        args.append(_normalize_group_name(group_name))
    if where:
        sql += " WHERE " + " AND ".join(where)
    return con.execute(sql, args).fetchone()[0]


def list_accounts(
    status: str = "", limit: int = 50, offset: int = 0, kind: str = "",
    group_name: str | None = None,
) -> list[dict]:
    con = _conn()
    sql = "SELECT * FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if group_name is not None and group_name != "__all__":
        where.append("group_name=?")
        args.append(_normalize_group_name(group_name))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY imported_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def stats_by_kind() -> dict:
    """按邮箱类型分组统计，给 WebUI 顶部展示"每种邮箱各有多少号"。"""
    con = _conn()
    cur = con.execute(
        "SELECT kind, status, COUNT(*) AS n FROM outlook_accounts GROUP BY kind, status"
    )
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        k = r["kind"] or "outlook"
        slot = out.setdefault(
            k, {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
        )
        slot[r["status"]] = r["n"]
        slot["total"] += r["n"]
    return out


def list_groups() -> list[dict]:
    """返回自定义分组及统计；未分组由调用方以空字符串表达。"""
    con = _conn()
    rows = con.execute(
        "SELECT g.name AS group_name, "
        "(SELECT COUNT(*) FROM outlook_accounts a WHERE a.group_name=g.name) AS total, "
        "(SELECT COUNT(*) FROM outlook_accounts a WHERE a.group_name=g.name "
        " AND a.status='available') AS available, "
        "(SELECT COUNT(*) FROM registered r WHERE r.group_name=g.name) AS registered_total, "
        "(SELECT COUNT(*) FROM registered r WHERE r.group_name=g.name "
        " AND COALESCE(r.account_status, 'active') <> 'permanently_invalid') AS active_registered_total "
        "FROM account_groups g ORDER BY g.name COLLATE NOCASE"
    ).fetchall()
    return [
        {
            "name": row["group_name"] or "",
            "total": row["total"],
            "available": row["available"] or 0,
            "registered_total": row["registered_total"] or 0,
            "active_registered_total": row["active_registered_total"] or 0,
        }
        for row in rows
    ]


def set_accounts_group(emails: list[str], group_name: str | None) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    group = _normalize_group_name(group_name)
    with _lock:
        con = _conn()
        if group:
            con.execute(
                "INSERT OR IGNORE INTO account_groups(name, created_at) VALUES (?, ?)",
                (group, time.time()),
            )
        rc = con.execute(
            f"UPDATE outlook_accounts SET group_name=? "
            f"WHERE email IN ({','.join('?' * len(cleaned))})",
            [group, *cleaned],
        )
        registered_rc = con.execute(
            f"UPDATE registered SET group_name=? "
            f"WHERE email IN ({','.join('?' * len(cleaned))})",
            [group, *cleaned],
        )
        con.commit()
        return max(rc.rowcount, registered_rc.rowcount)


def create_group(group_name: str) -> None:
    group = _normalize_group_name(group_name)
    if not group:
        raise ValueError("分组名称不能为空")
    with _lock:
        con = _conn()
        try:
            con.execute("INSERT INTO account_groups(name, created_at) VALUES (?, ?)", (group, time.time()))
        except sqlite3.IntegrityError:
            raise ValueError("该分组已存在")
        con.commit()


def rename_group(old_name: str, new_name: str) -> int:
    old = _normalize_group_name(old_name)
    new = _normalize_group_name(new_name)
    if not old or not new:
        raise ValueError("分组名称不能为空")
    if old == new:
        return 0
    with _lock:
        con = _conn()
        if not con.execute("SELECT 1 FROM account_groups WHERE name=?", (old,)).fetchone():
            raise ValueError("分组不存在")
        if con.execute("SELECT 1 FROM account_groups WHERE name=?", (new,)).fetchone():
            raise ValueError("目标分组已存在")
        con.execute("UPDATE account_groups SET name=? WHERE name=?", (new, old))
        rc = con.execute("UPDATE outlook_accounts SET group_name=? WHERE group_name=?", (new, old))
        registered_rc = con.execute("UPDATE registered SET group_name=? WHERE group_name=?", (new, old))
        con.commit()
        return max(rc.rowcount, registered_rc.rowcount)


def delete_group(group_name: str) -> int:
    group = _normalize_group_name(group_name)
    if not group:
        raise ValueError("不能删除未分组")
    with _lock:
        con = _conn()
        if not con.execute("SELECT 1 FROM account_groups WHERE name=?", (group,)).fetchone():
            raise ValueError("分组不存在")
        rc = con.execute("UPDATE outlook_accounts SET group_name='' WHERE group_name=?", (group,))
        registered_rc = con.execute("UPDATE registered SET group_name='' WHERE group_name=?", (group,))
        con.execute("DELETE FROM account_groups WHERE name=?", (group,))
        con.commit()
        return max(rc.rowcount, registered_rc.rowcount)


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM outlook_accounts WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_account(email: str) -> Optional[dict]:
    """原子 claim 指定邮箱（available / failed -> in_use）。

    failed 也允许重试 claim：之前 OpenAI 风控误判 / 网络抖动等导致 fail 的号
    应允许用户手动重试，已 done 的号才禁止重 claim（防误覆盖凭证）。

    按 email 指定时不过滤 kind —— 用户点名要这个号，它是什么类型
    由记录自己的 kind 列说了算，调用方读 account["kind"] 即可。
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        con = _conn()
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE email=? AND status IN ('available', 'failed')",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
            "WHERE email=? AND status IN ('available', 'failed')",
            (time.time(), email),
        )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def claim_next(kind: str = "", group_name: str | None = None) -> Optional[dict]:
    """原子 claim 任一 available 号。

    kind 指定 → 只从该类型里挑（"选了 gmail 就只跑 gmail 号"）
    group_name=None 或 __all__ → 全部分组；空串 → 仅未分组
    kind 为空 → 全池子里挑最早导入的

    多类型混放的关键就在这里：号池里 outlook 和 gmail 并存，
    但当前配置选了哪种，就只 claim 哪种，不会串。
    """
    k = (kind or "").strip().lower()
    group = None if group_name == "__all__" else _normalize_group_name(group_name) if group_name is not None else None
    with _lock:
        con = _conn()
        for _ in range(50):  # 有限重试，避免并发抢号时无限递归爆栈
            where, args = ["status='available'"], []
            if k:
                where.append("kind=?")
                args.append(k)
            if group is not None:
                where.append("group_name=?")
                args.append(group)
            cur = con.execute(
                "SELECT * FROM outlook_accounts WHERE " + " AND ".join(where)
                + " ORDER BY imported_at ASC LIMIT 1",
                args,
            )
            row = cur.fetchone()
            if not row:
                return None
            rc = con.execute(
                "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
                "WHERE email=? AND status='available'",
                (time.time(), row["email"]),
            )
            con.commit()
            if rc.rowcount == 1:
                return dict(row)
            # 被别的线程抢走了，换下一个再试
        return None


def mark_done(email: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL WHERE email=?",
            (time.time(), email.lower()),
        )
        con.commit()


def mark_failed(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_unused(email: str) -> None:
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """手动重置单个号：done / failed → available，清空时间戳和失败原因。

    场景：注册成功但 refresh_token 没拿到，主人想重新跑一遍这个号。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            "finished_at=NULL, fail_reason=NULL "
            "WHERE lower(email)=lower(?)",
            (email,),
        )
        con.commit()
        return rc.rowcount > 0


def bulk_reset_to_available(emails: list[str]) -> int:
    """批量重置多个号。返回实际被改的行数。"""
    if not emails:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            f"UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            f"finished_at=NULL, fail_reason=NULL "
            f"WHERE lower(email) IN ({','.join(['lower(?)'] * len(emails))})",
            emails,
        )
        con.commit()
        return rc.rowcount


def reset_failed_to_available() -> int:
    """把所有 failed 号一次性重置为 available（清掉 fail_reason）。返回受影响行数。

    场景：代理短暂抽风导致一波号被冤枉标 failed，主人想给它们一次机会。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', fail_reason=NULL, "
            "finished_at=NULL WHERE status='failed'"
        )
        con.commit()
        return rc.rowcount


def release_stale_in_use(stale_seconds: float = 1800) -> int:
    """把 claimed_at 超过 N 秒还在 in_use 的号释放回 available。

    场景：上次 webui 强退/进程崩溃，号卡在 in_use 永远不释放。默认 30 分钟。
    """
    with _lock:
        con = _conn()
        cutoff = time.time() - stale_seconds
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE status='in_use' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
        con.commit()
        return rc.rowcount


def delete_account(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_accounts_by_status(status: str) -> int:
    """按状态批量删除。status 必须是 available/in_use/done/failed 之一；
    传 'all' 删全部。返回受影响行数。"""
    valid = {"available", "in_use", "done", "failed", "all"}
    s = (status or "").strip().lower()
    if s not in valid:
        return 0
    with _lock:
        con = _conn()
        if s == "all":
            rc = con.execute("DELETE FROM outlook_accounts")
        else:
            rc = con.execute("DELETE FROM outlook_accounts WHERE status=?", (s,))
        con.commit()
        return rc.rowcount


def delete_accounts_by_emails(emails: list[str]) -> int:
    """按 email 列表批量删除。返回受影响行数。"""
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM outlook_accounts WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def stats() -> dict:
    con = _conn()
    cur = con.execute(
        "SELECT status, COUNT(*) AS n FROM outlook_accounts GROUP BY status"
    )
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


# ──────────────────────── 注册结果存储 ────────────────────────


def save_registered(d: dict) -> None:
    """保存注册成功（或部分成功）的凭证。覆盖同邮箱旧记录。

    凭证三件套（access_token / session_token / refresh_token）单独存列；
    其余字段（如 device_id / cookie_header / id_token / 自定义元数据）打包进 extra_json。
    """
    email = (d.get("email") or "").lower()
    if not email:
        return
    password = d.get("password", "") or ""
    extra = {k: v for k, v in d.items() if k not in {
        "email", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
        "totp_secret", "totp_factor_id", "group_name", "mail_kind",
    }}
    with _lock:
        con = _conn()
        pool_row = con.execute(
            "SELECT group_name, kind FROM outlook_accounts WHERE email=?", (email,)
        ).fetchone()
        existing_row = con.execute(
            "SELECT password, totp_secret, totp_factor_id, group_name, mail_kind, account_status "
            "FROM registered WHERE email=?", (email,)
        ).fetchone()
        group_name = (
            (pool_row["group_name"] or "") if pool_row
            else ((existing_row["group_name"] or "") if existing_row else "")
        )
        mail_kind = (
            (d.get("mail_kind") or "").strip()
            or ((pool_row["kind"] or "") if pool_row else "")
            or ((existing_row["mail_kind"] or "") if existing_row else "")
            or get_setting("mail_source", "outlook")
        )
        account_status = (
            (existing_row["account_status"] or "active")
            if existing_row else "active"
        )
        # ⚠️ INSERT OR REPLACE 是**整行替换**，不是按字段合并 —— 没写的列会被清空。
        #    重跑同一个邮箱时这会咬人：第一轮 register_password 设了密码但 OTP 超时，
        #    save_password_early 把密码存下了；第二轮 OpenAI 已经认识这个邮箱了，
        #    走 passwordless_login 分支根本不调 register_password，
        #    这一轮的 d["password"] 是空的 —— 直接 REPLACE 就把上一轮的密码冲没了。
        #    密码是 OpenAI 侧的**持久状态**，"这一轮没设" ≠ "这个号没有密码"，
        #    所以空值不覆盖非空旧值。
        #    token 三件套正相反：每轮跑都是全新的，旧的可能已失效，照常整列覆盖。
        # totp_secret 和密码同理，甚至更严：secret【一次性下发、服务端取不回】，
        #    丢了 = 该号 2FA 永久锁死。重跑同邮箱（已绑过 2FA）时这一轮不会再绑，
        #    d 里没有 secret —— 绝不能拿空值把库里已存的 secret 冲没。
        #    与密码合成一次 SELECT，顺带把两列旧值一起兜住。
        totp_secret = (d.get("totp_secret") or "").strip()
        totp_factor_id = (d.get("totp_factor_id") or "").strip()
        if existing_row:
            if not password and (existing_row["password"] or "").strip():
                password = existing_row["password"]
            if not totp_secret and (existing_row["totp_secret"] or "").strip():
                totp_secret = existing_row["totp_secret"]
                # factor_id 跟着 secret 走：本轮没绑就沿用旧的
                totp_factor_id = totp_factor_id or (existing_row["totp_factor_id"] or "")
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, group_name, mail_kind, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, account_status, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                group_name,
                mail_kind,
                password,
                d.get("access_token", ""),
                d.get("session_token", ""),
                d.get("refresh_token", ""),
                d.get("id_token", ""),
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                d.get("cookie_header", ""),
                totp_secret,
                totp_factor_id,
                account_status,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()


def update_registered_oauth_tokens(
    email: str, access_token: str = "", refresh_token: str = "", id_token: str = ""
) -> bool:
    """OAuth refresh 发生滚动后只更新 token，不触碰密码、2FA、session 等字段。"""
    email = str(email or "").strip().lower()
    if not email:
        return False
    sets = []
    values = []
    for column, value in (
        ("access_token", access_token),
        ("refresh_token", refresh_token),
        ("id_token", id_token),
    ):
        value = str(value or "").strip()
        if value:
            sets.append(f"{column}=?")
            values.append(value)
    if not sets:
        return False
    with _lock:
        con = _conn()
        values.append(email)
        rc = con.execute(
            f"UPDATE registered SET {', '.join(sets)} WHERE email=?", values
        )
        con.commit()
        return rc.rowcount > 0


def import_sub2api_registered(payload: object, group_name: str = "") -> dict:
    """导入 Sub2API 导出的已注册 OpenAI 账号。

    Sub2API 的 ``notes`` 当前是 JSON 字符串，但部分版本会直接写对象，
    两种形式都接受。导入只写 registered，不会把这些已注册账号放进待注册号池。
    """
    if isinstance(payload, dict):
        accounts = payload.get("accounts")
    elif isinstance(payload, list):
        accounts = payload
    else:
        raise ValueError("Sub2API 文件必须是对象或账号数组")
    if not isinstance(accounts, list):
        raise ValueError("Sub2API 文件缺少 accounts 数组")

    prepared = []
    errors = []
    for idx, account in enumerate(accounts, 1):
        if not isinstance(account, dict):
            errors.append({"line": idx, "error": "账号项不是对象"})
            continue
        credentials = account.get("credentials") or {}
        if not isinstance(credentials, dict):
            credentials = {}
        email = str(credentials.get("email") or account.get("email") or "").strip().lower()
        notes = account.get("notes") or {}
        if isinstance(notes, str):
            try:
                notes = json.loads(notes) if notes.strip() else {}
            except Exception:
                notes = {}
        if not isinstance(notes, dict):
            notes = {}
        gpt = notes.get("gpt") or {}
        two_factor = notes.get("two_factor") or {}
        mailbox = notes.get("mailbox") or {}
        if not isinstance(gpt, dict): gpt = {}
        if not isinstance(two_factor, dict): two_factor = {}
        if not isinstance(mailbox, dict): mailbox = {}
        password = str(gpt.get("password") or account.get("password") or "").strip()
        secret = str(two_factor.get("secret") or account.get("totp_secret") or "").strip()
        try:
            secret = normalize_totp_secret(secret) if secret else ""
        except ValueError as e:
            errors.append({"line": idx, "error": f"{email or '(无邮箱)'}: {e}"})
            continue
        if not email or "@" not in email:
            errors.append({"line": idx, "error": "缺少有效 credentials.email"})
            continue
        extra = {
            "sub2api_import": True,
            "sub2api_name": account.get("name", ""),
            "sub2api_type": account.get("type", ""),
            "sub2api_platform": account.get("platform", ""),
            "sub2api_extra": account.get("extra") or {},
            "sub2api_mailbox": mailbox,
        }
        prepared.append({
            "email": email,
            "password": password,
            "totp_secret": secret,
            "totp_factor_id": str(two_factor.get("factor_id") or "").strip(),
            "access_token": str(credentials.get("access_token") or ""),
            "refresh_token": str(credentials.get("refresh_token") or ""),
            "id_token": str(credentials.get("id_token") or ""),
            "group_name": group_name,
            "mail_kind": "outlook" if mailbox.get("refresh_token") else "",
            "extra": extra,
        })
    if errors:
        raise ValueError(json.dumps({"message": "Sub2API 导入校验失败", "errors": errors}, ensure_ascii=False))

    with _lock:
        con = _conn()
        for item in prepared:
            old = con.execute(
                "SELECT * FROM registered WHERE email=?", (item["email"],)
            ).fetchone()
            def keep(new, key):
                return new if new else ((old[key] or "") if old else "")
            con.execute(
                "INSERT OR REPLACE INTO registered "
                "(email, group_name, mail_kind, password, access_token, session_token, refresh_token, "
                "id_token, device_id, csrf_token, cookie_header, totp_secret, totp_factor_id, "
                "account_status, extra_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item["email"], item["group_name"] or ((old["group_name"] or "") if old else ""),
                    item["mail_kind"] or ((old["mail_kind"] or "") if old else ""),
                    keep(item["password"], "password"), keep(item["access_token"], "access_token"),
                    keep("", "session_token"), keep(item["refresh_token"], "refresh_token"),
                    keep(item["id_token"], "id_token"), keep("", "device_id"), keep("", "csrf_token"),
                    keep("", "cookie_header"), keep(item["totp_secret"], "totp_secret"),
                    keep(item["totp_factor_id"], "totp_factor_id"),
                    # 导入凭证不应解除账号的全局永久失效状态。Sub2API
                    # 导入使用 INSERT OR REPLACE，会整行替换，因此必须显式
                    # 保留旧状态；否则重新导入一次就会把失效账号恢复为 active。
                    ((old["account_status"] or "active") if old else "active"),
                    json.dumps(item["extra"], ensure_ascii=False), time.time(),
                ),
            )
            # Sub2API 示例同时携带 Outlook 收件箱凭证。同步写入邮箱表，
            # 这样仅登录在密码失败后仍能用该邮箱接收 OTP。
            mailbox = item["extra"].get("sub2api_mailbox") or {}
            mailbox_email = str(
                mailbox.get("bind_email") or mailbox.get("primary_email") or item["email"]
            ).strip().lower()
            mailbox_password = str(mailbox.get("password") or "").strip()
            mailbox_client_id = str(mailbox.get("client_id") or "").strip()
            mailbox_refresh = str(mailbox.get("refresh_token") or "").strip()
            if mailbox_email and mailbox_refresh:
                con.execute(
                    "INSERT INTO outlook_accounts "
                    "(email, password, client_id, refresh_token, kind, status, imported_at) "
                    "VALUES (?, ?, ?, ?, 'outlook', 'available', ?) "
                    "ON CONFLICT(email) DO UPDATE SET password=excluded.password, "
                    "client_id=excluded.client_id, refresh_token=excluded.refresh_token, "
                    "kind='outlook', status='available'",
                    (mailbox_email, mailbox_password, mailbox_client_id, mailbox_refresh, time.time()),
                )
        con.commit()
    return {"imported": len(prepared), "skipped": 0}


def save_password_early(email: str, password: str) -> None:
    """密码一在 OpenAI 侧生效就落盘，不等整个注册流程跑完。

    由 AuthFlow 的 on_password 回调触发（register_password 里 POST 200 之后）。
    此刻账号+密码在 OpenAI 那边已经建好，但本地还要过发码/验证/建账户三关，
    挂在任何一关都走不到 save_registered ——
    密码只活在内存里，进程一退号就成了谁也登不进去的孤儿。

    只写 email + password；token 三件套留空，等流程跑通后 save_registered
    用同一个 email 主键覆盖同一行补上。extra_json 打 pending 标记，
    方便一眼认出"有密码没凭证"的半成品行（跑通后会被 save_registered 清掉）。

    ⚠️ 行已存在时**只 UPDATE password**，绝不动已有的 token：
       重跑一个之前跑通过的邮箱时，不能把人家的凭证清空。
    """
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        return
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, group_name, mail_kind, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, COALESCE((SELECT group_name FROM outlook_accounts WHERE email=?), ''), "
            "COALESCE((SELECT kind FROM outlook_accounts WHERE email=?), "
            "(SELECT value FROM settings WHERE key='mail_source'), 'outlook'), "
            "?, '', '', '', '', '', '', '', ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET password=excluded.password",
            (
                email,
                email,
                email,
                password,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def save_totp_early(email: str, secret: str, factor_id: str = "") -> None:
    """2FA secret 一从 enroll 响应拿到就落盘，不等整个注册流程跑完。

    由 registrar 的 _bind_2fa_hook 触发（钩子在「拿到 session」和「Codex 授权 /
    绑手机号接码」之间调 bind_totp_2fa_inline，成功即拿到 secret）。

    ⚠️ 早落盘的理由和 save_password_early 一模一样、甚至更急：
       secret 绑成之后，流程还要走 Codex 授权 + add-phone 接码（可能好几分钟），
       这段时间 secret 只活在 registrar 内存的 _tfa_box 里。接码太久用户一关进程，
       secret 就永久蒸发 —— 而它【一次性下发、服务端取不回】，丢了该号 2FA 锁死。
       所以一拿到手就先写库，后面接码怎么中断都不怕。

    只写 totp 两列；token / 密码留给后续 save_registered 用同一 email 主键补齐。
    ⚠️ 行已存在时**只 UPDATE totp 两列**，绝不动已有的密码 / token
       —— 重跑老号时不能把人家已存的凭证清空。
    """
    email = (email or "").strip().lower()
    secret = (secret or "").strip()
    if not email or not secret:
        return
    factor_id = (factor_id or "").strip()
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, group_name, mail_kind, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, COALESCE((SELECT group_name FROM outlook_accounts WHERE email=?), ''), "
            "COALESCE((SELECT kind FROM outlook_accounts WHERE email=?), "
            "(SELECT value FROM settings WHERE key='mail_source'), 'outlook'), "
            "'', '', '', '', '', '', '', '', ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "totp_secret=excluded.totp_secret, "
            "totp_factor_id=excluded.totp_factor_id",
            (
                email,
                email,
                email,
                secret,
                factor_id,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def normalize_totp_secret(raw: str) -> str:
    """把用户手填的 TOTP secret 规范化成可用的 base32，非法值抛 ValueError。

    登录侧（auth_flow._totp_now）拿到 secret 直接 b32decode，**不做任何校验** ——
    脏值存进去要等到真登录时才炸，那时只看到一句 base32 解码异常，
    根本看不出是手填填错了。所以校验必须挡在写库这一关。

    接受的输入：
      - 裸 base32:  JBSWY3DPEHPK3PXP / jbswy3dp ehpk 3pxp / JBSW-Y3DP-EHPK
      - otpauth URI: otpauth://totp/ChatGPT:a@b.com?secret=JBSWY3DP&issuer=...
        （从手机 App 导出/二维码解码出来的就是这个格式，直接粘进来很常见）
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # otpauth:// URI 抽 secret 参数
    if s.lower().startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(s).query)
            s = (qs.get("secret") or [""])[0]
        except Exception:
            raise ValueError("otpauth 链接解析失败，请直接填 secret")
        if not s:
            raise ValueError("otpauth 链接里没有 secret 参数")
    # 去掉分隔符（手机 App 展示时常带空格/连字符）并统一大写
    s = s.replace(" ", "").replace("-", "").replace("_", "").upper()
    # base32 只有 A-Z 和 2-7，先挡掉明显非法字符再解码，报错更好懂
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s):
        raise ValueError("TOTP secret 含非法字符（base32 只允许 A-Z 和 2-7）")
    try:
        # 补 padding 后试解，解得开才算合法。auth_flow 那边也是这么补的。
        decoded = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise ValueError("TOTP secret 不是合法的 base32")
    if len(decoded) < 10:
        raise ValueError(f"TOTP secret 太短（解出 {len(decoded)} 字节，通常应为 20 字节）")
    return s


def update_registered_manual(email: str, password: Optional[str] = None,
                             totp_secret: Optional[str] = None) -> bool:
    """手动修正某个已注册账号的密码 / TOTP secret。

    ⚠️ 只改**本地库**，不会同步到 OpenAI —— 这里改密码不等于改了账号密码。
       用途是把外部已知的凭证补进来，或修正记录错误。

    传 None = 该字段不动（不是清空）。用 None 而不是空串做"不修改"的标记，
    是为了留出"主人真想清空某字段"的余地（传空串即清空）。

    totp_secret 会先过 normalize_totp_secret 校验，非法直接抛 ValueError；
    宁可这里报错，也不能让脏值躺进库里等登录时才炸。

    返回 False 表示该邮箱不存在（不会凭空插入新行 —— 手填是"修正已有记录"，
    真要新增外部账号是另一件事，走单独的导入功能）。
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    sets, vals = [], []
    if password is not None:
        sets.append("password=?")
        vals.append(password)
    if totp_secret is not None:
        # 空串 = 主人主动清空；非空则必须过校验
        sets.append("totp_secret=?")
        vals.append(normalize_totp_secret(totp_secret) if totp_secret.strip() else "")
    if not sets:
        return False
    with _lock:
        con = _conn()
        row = con.execute("SELECT email FROM registered WHERE email=?", (email,)).fetchone()
        if not row:
            return False
        vals.append(email)
        con.execute(f"UPDATE registered SET {', '.join(sets)} WHERE email=?", vals)
        con.commit()
    return True


def mark_registered_permanently_invalid(email: str, reason: str = "") -> bool:
    """将账号标记为全局永久失效，并同步所有空间候选关系。

    只更新候选的账号失效兼容状态，不触碰 workspace_join_status、member_id
    或席位字段；账号失效与是否仍位于空间是两个独立事实。
    """
    email = str(email or "").strip().lower()
    if not email:
        return False
    with _lock:
        con = _conn()
        row = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,)).fetchone()
        if not row:
            return False
        try:
            extra = json.loads(row["extra_json"] or "{}")
            if not isinstance(extra, dict):
                extra = {}
        except Exception:
            extra = {}
        extra["permanently_invalid"] = True
        if reason:
            extra["permanently_invalid_reason"] = str(reason)[:500]
        con.execute(
            "UPDATE registered SET account_status='permanently_invalid', extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.execute(
            "UPDATE workspace_candidates SET status='permanently_invalid', updated_at=? WHERE email=?",
            (time.time(), email),
        )
        con.commit()
        return True


def list_registered_invalid_emails(emails: list[str]) -> set[str]:
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not cleaned:
        return set()
    marks = ",".join("?" * len(cleaned))
    rows = _conn().execute(
        f"SELECT email FROM registered WHERE account_status='permanently_invalid' AND email IN ({marks})",
        cleaned,
    ).fetchall()
    return {str(row["email"]).lower() for row in rows}

def _canonical_workspace_seat_type(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"usage_based", "usagebased", "codex"}:
        return "usage_based"
    if normalized in {"default", "standard", "standard_seat", "gpt"}:
        return "default"
    if "codex席位" in normalized:
        return "usage_based"
    if "gpt席位" in normalized or "标准席位" in normalized:
        return "default"
    return normalized

def update_workspace_candidate_seats(workspace_master_id: int, email: str, codex_seat: str = "", gpt_seat: str = "") -> None:
    with _lock:
        con = _conn()
        value = codex_seat or gpt_seat
        seat_type = _canonical_workspace_seat_type(value)
        con.execute(
            "UPDATE workspace_candidates SET seat_type=?, codex_seat=?, gpt_seat=?, updated_at=? WHERE workspace_master_id=? AND email=?",
            (seat_type, codex_seat or "", gpt_seat or "", time.time(), int(workspace_master_id), str(email).lower()),
        )
        con.commit()


def update_plus_check(email: str, plus_info: dict) -> None:
    """把 Plus 检查结果写入 extra_json.plus_check。"""
    email = email.lower()
    con = _conn()
    cur = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return
    extra = {}
    if row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except Exception:
            extra = {}
    extra["plus_check"] = plus_info
    with _lock:
        con.execute(
            "UPDATE registered SET extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.commit()

def update_workspace_candidate_member(workspace_master_id: int, email: str, member_id: str, seat_type: str) -> None:
    with _lock:
        con = _conn()
        wid = int(workspace_master_id)
        key = str(email).lower()
        member = str(member_id or "").strip()
        con.execute(
            "UPDATE workspace_candidates SET member_id=?, seat_type=?, updated_at=? WHERE workspace_master_id=? AND email=?",
            (member, seat_type, time.time(), wid, key),
        )
        if member:
            con.execute(
                "UPDATE workspace_candidates SET workspace_join_status='joined', updated_at=? WHERE workspace_master_id=? AND email=?",
                (time.time(), wid, key),
            )
        con.commit()


def get_workspace_candidate_seat_type(workspace_master_id: int, email: str) -> str:
    """返回候选人在指定空间的原始席位类型。"""
    row = _conn().execute(
        "SELECT seat_type FROM workspace_candidates WHERE workspace_master_id=? AND email=?",
        (int(workspace_master_id), str(email or "").strip().lower()),
    ).fetchone()
    return str(row["seat_type"] or "") if row else ""


def _registered_conditions(filt: str, group_name: str | None = None) -> tuple[str, list]:
    conditions: list[str] = []
    args: list = []
    if filt == "has_rt":
        conditions.append("length(refresh_token) > 0")
    elif filt == "no_rt":
        conditions.append("coalesce(length(refresh_token),0) = 0")
    elif filt == "unchecked":
        conditions.append("(extra_json IS NULL OR extra_json NOT LIKE '%\"plus_check\"%')")
    elif filt == "free":
        conditions.append("extra_json LIKE '%\"free\"%'")
    elif filt == "plus":
        conditions.append("(extra_json LIKE '%\"plus_eligible\"%' OR extra_json LIKE '%\"plus_active\"%')")
    elif filt == "banned":
        conditions.append("extra_json LIKE '%\"banned\"%'")
    elif filt == "token_invalid":
        # token_invalid 从 2026-08-10 起会写库，得能筛出来，否则等于埋了：
        # 它既不在 unchecked 里（已有结论），又不在 free/plus/banned 里。
        conditions.append("extra_json LIKE '%\"token_invalid\"%'")
    if group_name is not None and group_name != "__all__":
        conditions.append("group_name=?")
        args.append(_normalize_group_name(group_name))
    return (("WHERE " + " AND ".join(conditions)) if conditions else "", args)


def count_registered(filter_rt: str = "all", group_name: str | None = None) -> int:
    con = _conn()
    where, args = _registered_conditions(filter_rt, group_name)
    cur = con.execute(f"SELECT COUNT(*) FROM registered {where}", args)
    return cur.fetchone()[0]


def list_registered(
    limit: int = 20, offset: int = 0, filter_rt: str = "all",
    group_name: str | None = None,
) -> list[dict]:
    con = _conn()
    where, args = _registered_conditions(filter_rt, group_name)
    cur = con.execute(
        f"SELECT email, group_name, "
        f"CASE WHEN account_status='permanently_invalid' THEN '' ELSE password END AS password, "
        f"CASE WHEN account_status='permanently_invalid' THEN '' ELSE totp_secret END AS totp_secret, account_status, "
        f"CASE WHEN account_status='permanently_invalid' THEN 0 ELSE length(access_token) END AS at_len, "
        f"CASE WHEN account_status='permanently_invalid' THEN 0 ELSE length(session_token) END AS st_len, "
        f"CASE WHEN account_status='permanently_invalid' THEN 0 ELSE length(refresh_token) END AS rt_len, extra_json, created_at FROM registered "
        f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*args, limit, offset],
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        plus = None
        if d.get("extra_json"):
            try:
                extra = json.loads(d["extra_json"])
                plus = extra.get("plus_check")
            except Exception:
                pass
        d["plus_check"] = plus
        d.pop("extra_json", None)
        rows.append(d)
    return rows


def list_login_candidates(group_name: str | None = "", filter_rt: str = "all") -> list[dict]:
    """为一次“仅登录”任务生成稳定快照，每个注册结果最多出现一次。"""
    where = ""
    args: list = []
    conditions = ["COALESCE(r.account_status, 'active') <> 'permanently_invalid'"]
    if filter_rt == "has_rt":
        conditions.append("length(r.refresh_token) > 0")
    elif filter_rt == "no_rt":
        conditions.append("coalesce(length(r.refresh_token), 0) = 0")
    if group_name is not None and group_name != "__all__":
        conditions.append("r.group_name=?")
        args.append(_normalize_group_name(group_name))
    where = "WHERE " + " AND ".join(conditions)
    con = _conn()
    rows = con.execute(
        "SELECT r.email, r.password AS login_password, r.totp_secret, "
        "r.group_name, r.mail_kind, "
        "a.password AS mail_password, a.client_id, a.refresh_token, "
        "a.relay_url, a.kind AS pool_kind "
        "FROM registered r LEFT JOIN outlook_accounts a ON a.email=r.email "
        f"{where} ORDER BY r.created_at ASC",
        args,
    ).fetchall()
    return [
        {
            "email": row["email"],
            "password": row["mail_password"] or "",
            "client_id": row["client_id"] or "",
            "refresh_token": row["refresh_token"] or "",
            "relay_url": row["relay_url"] or "",
            "kind": row["pool_kind"] or row["mail_kind"] or "outlook",
            "login_password": row["login_password"] or "",
            "totp_secret": row["totp_secret"] or "",
            "group_name": row["group_name"] or "",
        }
        for row in rows
    ]


def list_registered_full(limit: int = 5000) -> list[dict]:
    """返回完整凭证（用于批量导出）。每行同 get_registered 的格式，外加 relay_url。

    ⚠️ relay_url（中转取件链接）**不在 registered 表里**，它跟着号池那一行走
       （outlook_accounts.relay_url，icloud_relay 这类号一号一条 token）。
       导出格式「邮箱----密码----2FA----取件url」要用它，所以这里 LEFT JOIN 带出来。
       用 JOIN 而不是给 registered 加列的原因：不用迁移、**已经注册完的老号也能导**
       （只要号池那行还在）；号池行被删掉就是空串，照约定留空、分隔符保留。
    """
    con = _conn()
    cur = con.execute(
        "SELECT r.*, a.relay_url AS relay_url, a.password AS mail_password, "
        "a.client_id AS mail_client_id, a.refresh_token AS mail_refresh_token, "
        "a.kind AS pool_kind "
        "FROM registered r LEFT JOIN outlook_accounts a ON a.email = r.email "
        "ORDER BY r.created_at DESC LIMIT ?",
        (limit,),
    )
    out = []
    for row in cur.fetchall():
        d = dict(row)
        if d.get("extra_json"):
            try:
                d["extra"] = json.loads(d["extra_json"])
            except Exception:
                d["extra"] = {}
        d.pop("extra_json", None)
        _mask_invalid_registered_credentials(d)
        out.append(d)
    return out


def list_registered_by_emails(emails: list[str]) -> list[dict]:
    """按 email 列表返回完整凭证（批量导出勾选的号用）。

    - 行序 = created_at 倒序，和「注册结果」表格里看到的一致，方便核对。
    - 查不到的 email 直接不出现（号已被删掉的情况），不报错。
    - SQLite 单条语句变量数有上限（默认 999），所以分批查。
    - relay_url 从号池表 LEFT JOIN 带出（原因见 list_registered_full）。
    """
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return []

    con = _conn()
    out = []
    CHUNK = 500
    for i in range(0, len(cleaned), CHUNK):
        part = cleaned[i:i + CHUNK]
        placeholders = ",".join("?" * len(part))
        cur = con.execute(
            f"SELECT r.*, a.relay_url AS relay_url, a.password AS mail_password, "
            f"a.client_id AS mail_client_id, a.refresh_token AS mail_refresh_token, "
            f"a.kind AS pool_kind "
            f"FROM registered r LEFT JOIN outlook_accounts a ON a.email = r.email "
            f"WHERE r.email IN ({placeholders})",
            part,
        )
        for row in cur.fetchall():
            d = dict(row)
            if d.get("extra_json"):
                try:
                    d["extra"] = json.loads(d["extra_json"])
                except Exception:
                    d["extra"] = {}
            d.pop("extra_json", None)
            _mask_invalid_registered_credentials(d)
            out.append(d)

    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out


def get_registered(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM registered WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    if not row:
        return None
    out = dict(row)
    if out.get("extra_json"):
        try:
            out["extra"] = json.loads(out["extra_json"])
        except Exception:
            out["extra"] = {}
    out.pop("extra_json", None)
    _mask_invalid_registered_credentials(out)
    return out


def _mask_invalid_registered_credentials(row: dict) -> dict:
    """只在读取层撤销永久失效账号的凭证，不改数据库原始记录。"""
    if row.get("account_status") != "permanently_invalid":
        return row
    for field in (
        "password", "totp_secret", "totp_factor_id", "access_token",
        "session_token", "refresh_token", "id_token", "device_id",
        "csrf_token", "cookie_header", "relay_url", "mail_password",
        "mail_refresh_token", "workspace_access_token", "workspace_session_token",
        "workspace_refresh_token", "workspace_id_token", "workspace_device_id",
        "workspace_csrf_token", "workspace_cookie_header",
    ):
        if field in row:
            row[field] = ""
    for field in ("at_len", "st_len", "rt_len"):
        if field in row:
            row[field] = 0
    return row


def _workspace_candidate_join_status_expr(candidate_alias: str = "c", credential_alias: str = "wc") -> str:
    return (
        f"CASE WHEN COALESCE({candidate_alias}.workspace_join_status, '') = 'joined' THEN 'joined' "
        f"WHEN COALESCE({candidate_alias}.member_id, '') <> '' THEN 'joined' "
        f"WHEN length(COALESCE({credential_alias}.access_token, '')) > 0 THEN 'joined' "
        f"ELSE COALESCE({candidate_alias}.workspace_join_status, 'not_invited') END"
    )


def _repair_workspace_candidate_join_statuses(con: sqlite3.Connection) -> int:
    """回填历史上被误写成未加入的候选记录。"""
    now = time.time()
    rc = con.execute(
        """
        UPDATE workspace_candidates
           SET workspace_join_status='joined', updated_at=?
         WHERE workspace_join_status<>'joined'
           AND (
                COALESCE(member_id, '') <> ''
                OR EXISTS (
                    SELECT 1
                      FROM workspace_credentials wc
                     WHERE wc.workspace_master_id = workspace_candidates.workspace_master_id
                       AND wc.email = workspace_candidates.email
                       AND length(COALESCE(wc.access_token, '')) > 0
                )
           )
        """,
        (now,),
    )
    return rc.rowcount


def delete_registered(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_registered_by_emails(emails: list[str]) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM registered WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def delete_all_registered() -> int:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered")
        con.commit()
        return rc.rowcount


# ──────────────────────── 运行记录 ────────────────────────


def create_run(run_id: str, email: str, log_path: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO runs(run_id, email, status, started_at, log_path) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, email.lower(), time.time(), log_path),
        )
        con.commit()


def finish_run(run_id: str, status: str, error: str = "", category: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runs SET status=?, finished_at=?, error=?, error_category=? WHERE run_id=?",
            (status, time.time(), (error or "")[:500], category or None, run_id),
        )
        con.commit()


def list_runs(limit: int = 50) -> list[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ──────────────────────── settings (KV) ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    con = _conn()
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()


def _setting_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _setting_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


# ──────────────────────── 邮箱来源配置 ────────────────────────


def get_mail_config() -> dict:
    """返回邮箱来源配置（密码类字段隐藏明文）。

    provider 声明的配置项自动带出来 —— 加新邮箱时这里不用改，
    新 provider 的 config_fields 会自动出现在返回值里。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if f.get("type") == "password":
                out[key] = "***" if get_setting(key) else ""
            else:
                out[key] = get_setting(key, "")
    return out


def save_mail_config(data: dict) -> None:
    """保存邮箱配置。password 类字段传 '***' 表示不修改。

    mail_source 校验改成查 mail_providers 注册表：
        以前是写死的白名单 ("outlook", "cf_temp")，选了别的会被
        **静默改回 outlook** —— 用户看到的是"保存成功但选择没生效"。
        现在未知来源直接抛错，问题当场暴露。
    """
    from mail_providers import get_provider_class, list_providers

    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        get_provider_class(src)  # 未注册的 kind 会抛 MailProviderError
        set_setting("mail_source", src)

    # 按 provider 声明的字段保存，加新邮箱时这里零改动
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if key not in data:
                continue
            val = data[key]
            if f.get("type") == "password":
                if not val or val == "***":
                    continue  # 没填 / 是掩码 → 保持原值
            set_setting(key, str(val).strip())


def get_secret_setting(key: str) -> str:
    """内部用：拿密码类配置的明文。"""
    return get_setting(key, "")


def get_mail_settings() -> dict:
    """内部用：给 create_mail_provider 的 settings（含明文密钥）。

    跟 get_mail_config 的区别：这个不打码，只在服务端构造 provider 时用，
    绝不能直接返回给前端。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            out[f["key"]] = get_setting(f["key"], "")
    return out


def get_cf_admin_token() -> str:
    """内部用：拿明文 admin_token。"""
    return get_setting("cf_admin_token", "")


# ──────────────────────── SMS 接码配置 ────────────────────────


def get_sms_config() -> dict:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    sms_enabled:        '0'/'1' 是否启用接码（命中 add-phone 时才会用）
    sms_provider:       smsbower
    sms_country:        国家代码或 ID（推荐 '52' = Thailand，OpenAI 走 SMS 的唯一稳定国家）
    sms_service:        服务代码（OpenAI = 'dr'）
    sms_max_price:      号码最高单价（SmsBower / SmsBower 用，单位平台货币；空 / -1 = 不限）
    sms_reuse_phone:    '0'/'1' 同号复用（SmsBower / SmsBower 支持，省钱）
    sms_phone_success_max: 同号最多复用几次（默认 3）
    sms_auto_country:   '0'/'1' 自动选最优国家（按价格 + 库存）
    sms_auto_min_stock: 自动选国家最低库存（默认 20）
    sms_auto_max_price: 自动选国家最高单价（默认 0 = 不限）
    """
    return {
        "sms_enabled":             get_setting("sms_enabled", "0"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             "***" if get_setting("sms_api_key") else "",
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


def save_sms_config(data: dict) -> None:
    """保存 SMS 配置。sms_api_key 传 '***' 表示不修改。"""
    # 校验 provider
    valid_providers = {"smsbower", "herosms"}
    if "sms_provider" in data:
        p = str(data["sms_provider"]).strip().lower()
        if p not in valid_providers:
            p = "smsbower"
        set_setting("sms_provider", p)
    # 字符串字段直接落
    for key in (
        "sms_country", "sms_service", "sms_max_price", "sms_fixed_price",
        "sms_phone_success_max", "sms_auto_min_stock", "sms_auto_max_price",
        "sms_max_phone_attempts", "sms_per_phone_timeout",
        "sms_allowed_countries",
    ):
        if key in data:
            set_setting(key, str(data[key]).strip())
    # 布尔字段（前端传 '0'/'1' 或 bool）
    for key in ("sms_enabled", "sms_reuse_phone", "sms_auto_country", "sms_strict_whitelist"):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    # API key（'***' 不修改）
    if data.get("sms_api_key") and data["sms_api_key"] != "***":
        set_setting("sms_api_key", str(data["sms_api_key"]).strip())


def get_sms_internal_config() -> dict:
    """内部用：拿明文 sms_api_key,供 sms_provider 实例化使用。"""
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             get_setting("sms_api_key", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0") in ("1", "true"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0") in ("1", "true"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0") in ("1", "true"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


# ──────────────────────── 自动导出配置 (CPA / SUB2API) ────────────────────────


def get_export_config() -> dict:
    """返回导出配置（敏感字段做明文/'***' 占位）。

    给前端展示用：
      cpa_mgmt_key / sub2api_api_key 已设置时返回 '***'，未设置返回 ''。
      保存时传 '***' 代表不修改。
    """
    return {
        # CPA
        "cpa_enabled":     get_setting("export_cpa_enabled", "0"),
        "cpa_url":         get_setting("export_cpa_url", ""),
        "cpa_mgmt_key":    "***" if get_setting("export_cpa_mgmt_key") else "",
        "cpa_timeout":     get_setting("export_cpa_timeout", "30"),
        # SUB2API
        "sub2api_enabled":    get_setting("export_sub2api_enabled", "0"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    "***" if get_setting("export_sub2api_api_key") else "",
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }


def save_export_config(data: dict) -> None:
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    # 布尔开关
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
    ):
        if key_in in data:
            v = data[key_in]
            if isinstance(v, bool):
                set_setting(key_out, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key_out, "1" if s in ("1", "true", "yes", "on") else "0")
    # 字符串字段（明文）
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, _setting_text(data[key_in]))
    # 密文字段（'***' 不修改）
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。供 registrar / app.test 调用。

    返回两个子配置 dict，可分别传给 exporter.export_to_cpa / export_to_sub2api。
    """
    cpa = {
        "enabled":      get_setting("export_cpa_enabled", "0") in ("1", "true"),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":            get_setting("export_sub2api_enabled", "0") in ("1", "true"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


def get_public_relogin_config() -> dict:
    """公开 401 重登录页面的后台配置。"""
    return {
        "enabled": get_setting("public_relogin_enabled", "0"),
        "proxy_pool": get_setting("public_relogin_proxy_pool", ""),
        "use_system_proxy_pool": get_setting("public_relogin_use_system_proxy_pool", "1"),
        "concurrency": get_setting("public_relogin_concurrency", "3"),
        "retry_count": get_setting("public_relogin_retry_count", "2"),
        "quota_timeout": get_setting("public_relogin_quota_timeout", "30"),
        "login_timeout": get_setting("public_relogin_login_timeout", "180"),
    }


def save_public_relogin_config(data: dict) -> None:
    if "public_relogin_enabled" in data:
        set_setting("public_relogin_enabled", "1" if _setting_bool(data["public_relogin_enabled"]) else "0")
    for key_in, key_out in (
        ("proxy_pool", "public_relogin_proxy_pool"),
        ("use_system_proxy_pool", "public_relogin_use_system_proxy_pool"),
        ("concurrency", "public_relogin_concurrency"),
        ("retry_count", "public_relogin_retry_count"),
        ("quota_timeout", "public_relogin_quota_timeout"),
        ("login_timeout", "public_relogin_login_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, _setting_text(data[key_in]))


_PUBLIC_RELOGIN_ACCESS_KEYS_SETTING = "public_relogin_access_keys"


def _public_relogin_key_hash(raw_key: str) -> str:
    return hashlib.sha256(str(raw_key or "").strip().encode("utf-8")).hexdigest()


def _load_public_relogin_access_keys() -> list[dict]:
    raw = get_setting(_PUBLIC_RELOGIN_ACCESS_KEYS_SETTING, "[]")
    try:
        data = json.loads(raw or "[]")
    except Exception:
        data = []
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append({
            "id": str(item.get("id") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "prefix": str(item.get("prefix") or "").strip(),
            "key_hash": str(item.get("key_hash") or "").strip(),
            "created_at": float(item.get("created_at") or 0),
            "expires_at": float(item.get("expires_at") or 0),
            "last_used_at": float(item.get("last_used_at") or 0),
            "revoked": bool(item.get("revoked") or False),
        })
    return [row for row in rows if row["id"] and row["key_hash"]]


def _save_public_relogin_access_keys(rows: list[dict]) -> None:
    set_setting(_PUBLIC_RELOGIN_ACCESS_KEYS_SETTING, json.dumps(rows, ensure_ascii=False, separators=(",", ":")))


def _public_relogin_access_key_view(row: dict) -> dict:
    now = time.time()
    expires_at = float(row.get("expires_at") or 0)
    revoked = bool(row.get("revoked") or False)
    expired = bool(expires_at and expires_at <= now)
    return {
        "id": row.get("id") or "",
        "name": row.get("name") or "",
        "prefix": row.get("prefix") or "",
        "created_at": float(row.get("created_at") or 0),
        "expires_at": expires_at,
        "last_used_at": float(row.get("last_used_at") or 0),
        "revoked": revoked,
        "expired": expired,
        "active": (not revoked) and (not expired),
    }


def list_public_relogin_access_keys() -> list[dict]:
    return [_public_relogin_access_key_view(row) for row in _load_public_relogin_access_keys()]


def create_public_relogin_access_key(name: str = "", expires_at: float = 0) -> dict:
    raw_key = "prk_" + secrets.token_urlsafe(32)
    now = time.time()
    row = {
        "id": secrets.token_urlsafe(12),
        "name": str(name or "").strip()[:80],
        "prefix": raw_key[:12],
        "key_hash": _public_relogin_key_hash(raw_key),
        "created_at": now,
        "expires_at": float(expires_at or 0),
        "last_used_at": 0,
        "revoked": False,
    }
    rows = _load_public_relogin_access_keys()
    rows.insert(0, row)
    _save_public_relogin_access_keys(rows)
    return {**_public_relogin_access_key_view(row), "key": raw_key}


def revoke_public_relogin_access_key(key_id: str) -> bool:
    key_id = str(key_id or "").strip()
    rows = _load_public_relogin_access_keys()
    changed = False
    for row in rows:
        if row.get("id") == key_id:
            row["revoked"] = True
            changed = True
            break
    if changed:
        _save_public_relogin_access_keys(rows)
    return changed


def validate_public_relogin_access_key(raw_key: str) -> dict | None:
    if not str(raw_key or "").strip():
        return None
    key_hash = _public_relogin_key_hash(raw_key)
    now = time.time()
    rows = _load_public_relogin_access_keys()
    matched: dict | None = None
    for row in rows:
        if row.get("key_hash") != key_hash:
            continue
        expires_at = float(row.get("expires_at") or 0)
        if row.get("revoked") or (expires_at and expires_at <= now):
            return None
        row["last_used_at"] = now
        matched = row
        break
    if matched:
        _save_public_relogin_access_keys(rows)
        return _public_relogin_access_key_view(matched)
    return None


def get_admin_auth_config() -> dict:
    return {
        "admin_password_hash": get_setting("admin_password_hash", ""),
    }


def save_admin_auth_config(data: dict) -> None:
    if "admin_password" not in data:
        return
    raw = str(data.get("admin_password") or "").strip()
    if not raw:
        set_setting("admin_password_hash", "")
        return
    import hashlib as _hashlib
    set_setting("admin_password_hash", _hashlib.sha256(raw.encode("utf-8")).hexdigest())


# 模块加载时自动建表
init_db()
