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
    # Workspace child rows must be removed with their deleted master.  Without
    # this connection-local pragma, SQLite silently leaves orphan candidates
    # behind and the background trash sweeper can keep using a deleted master.
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outlook_accounts (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            login_password  TEXT,       -- 手动录入的 OpenAI 登录密码（不覆盖邮箱收件密码）
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
            seats_default_entitled INTEGER,
            seats_usage_based INTEGER,
            seats_prolite   INTEGER,
            seats_prolite_entitled INTEGER,
            seats_default_available INTEGER,
            seats_prolite_available INTEGER,
            seats_default_held INTEGER,
            seats_prolite_held INTEGER,
            will_renew      INTEGER NOT NULL DEFAULT 1,
            is_delinquent   INTEGER NOT NULL DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS proxy_lease_usage (
            proxy           TEXT NOT NULL,
            task_type       TEXT NOT NULL,
            task_detail     TEXT NOT NULL DEFAULT '',
            leased_count    INTEGER NOT NULL DEFAULT 0,
            first_leased_at REAL NOT NULL,
            last_leased_at  REAL NOT NULL,
            PRIMARY KEY (proxy, task_type, task_detail)
        );

        CREATE INDEX IF NOT EXISTS idx_proxy_lease_usage_last
            ON proxy_lease_usage(last_leased_at DESC);

        CREATE TABLE IF NOT EXISTS proxy_cooldown (
            proxy           TEXT NOT NULL,
            error_type      TEXT NOT NULL,
            error_count     INTEGER NOT NULL DEFAULT 0,
            last_error_at   REAL NOT NULL DEFAULT 0,
            cooldown_until  REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (proxy, error_type)
        );

        CREATE INDEX IF NOT EXISTS idx_proxy_cooldown_until
            ON proxy_cooldown(cooldown_until DESC);

        -- CPA 静态家宽代理的租用绑定：一行 = 一个候选人在本空间占着一个
        -- 家宽代理。池子的「计数」就是同一 proxy 值的行数，取最少计数的
        -- 代理分配；凭证从 CPA 删除（或视作删除）后删行即计数-1。
        CREATE TABLE IF NOT EXISTS cpa_proxy_leases (
            workspace_master_id INTEGER NOT NULL,
            email           TEXT NOT NULL,
            proxy           TEXT NOT NULL DEFAULT '',
            created_at      REAL NOT NULL,
            PRIMARY KEY (workspace_master_id, email)
        );

        -- 公开重登录页的惩罚记录。那边的账号只存在用户浏览器里，后端没有任何
        -- 按账号的持久行（不像候选人有 workspace_credentials），403 连击和 402
        -- 判死都得自己找地方落库。
        --   account_403    subject=email      detail=''     连击缓刑计数
        --   workspace_402  subject=workspace  detail=email  一行一个账号，
        --                  COUNT(*) 就是「吃到 402 的不同账号数」
        --   workspace_dead subject=workspace  detail=''     判死墓碑，永久有效
        CREATE TABLE IF NOT EXISTS public_relogin_penalty (
            scope       TEXT NOT NULL,
            subject     TEXT NOT NULL,
            detail      TEXT NOT NULL DEFAULT '',
            count       INTEGER NOT NULL DEFAULT 0,
            first_at    REAL NOT NULL DEFAULT 0,
            last_at     REAL NOT NULL DEFAULT 0,
            note        TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (scope, subject, detail)
        );

        CREATE INDEX IF NOT EXISTS idx_public_relogin_penalty_scope
            ON public_relogin_penalty(scope, subject);

        -- 注册追溯：每个账号一行，独立于 registered 凭证表。
        -- registered 承载「当前凭证」，INSERT OR REPLACE 会整行覆盖 created_at，
        -- 且账号被删除后行就没了；追溯要的是首次注册时间 / 出口 IP / 失败
        -- 累计这些【历史事实】，凭证行的生命周期不该影响它，所以单开一张表。
        -- 失败计数也覆盖「注册失败、从未成功过」的账号（registered 里查不到）。
        CREATE TABLE IF NOT EXISTS register_trace (
            email              TEXT PRIMARY KEY,
            register_ip        TEXT NOT NULL DEFAULT '',
            register_region    TEXT NOT NULL DEFAULT '',
            register_mode      TEXT NOT NULL DEFAULT '',
            register_timezone  TEXT NOT NULL DEFAULT '',
            register_language  TEXT NOT NULL DEFAULT '',
            registered_at      REAL,
                            -- 首次成功注册时间；只设一次，重跑不覆盖
            last_success_at    REAL,
            fail_count         INTEGER NOT NULL DEFAULT 0,
            last_fail_at       REAL,
            last_fail_error    TEXT NOT NULL DEFAULT '',
            last_fail_category TEXT NOT NULL DEFAULT '',
            last_run_id        TEXT NOT NULL DEFAULT '',
            created_at         REAL NOT NULL,
            updated_at         REAL NOT NULL
        );

        -- 个人空间（Free 账号池）：从 registered 划入的免费账号。
        -- 与 workspace_candidates 对应但去掉所有 Team 语义——没有母号、
        -- 没有席位、没有上游成员关系；凭证直接读 registered 的个人 token。
        CREATE TABLE IF NOT EXISTS personal_candidates (
            email           TEXT PRIMARY KEY,
            trash_status    TEXT NOT NULL DEFAULT 'active',
            trash_due_at    REAL NOT NULL DEFAULT 0,
            trash_reason    TEXT NOT NULL DEFAULT '',
            quota_json      TEXT,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );
    """)
    con.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES ('proxy_usage_since', ?)",
        (str(time.time()),),
    )
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
        ("seats_default_entitled", "INTEGER"),
        ("seats_usage_based", "INTEGER"),
        ("seats_prolite", "INTEGER"),
        ("seats_prolite_entitled", "INTEGER"),
        ("seats_default_available", "INTEGER"),
        ("seats_prolite_available", "INTEGER"),
        ("seats_default_held", "INTEGER"),
        ("seats_prolite_held", "INTEGER"),
        # 未同步过的旧母号按"会续订、不欠费"处理，与改造前页面的隐含假设一致。
        ("will_renew", "INTEGER NOT NULL DEFAULT 1"),
        ("is_delinquent", "INTEGER NOT NULL DEFAULT 0"),
        ("seat_cost", "TEXT NOT NULL DEFAULT ''"),
        ("renewal_date", "TEXT NOT NULL DEFAULT ''"),
        ("settings_json", "TEXT NOT NULL DEFAULT '{}'"),
        # OpenAI 现在把会话绑定到登录时的设备指纹（oai-device-id/UA）：
        # 指纹不匹配时 /api/auth/session 只会返回已失效会话的缓存 token，
        # 管理请求拿到就是 401 token_invalidated。导入的 session JSON 自带
        # statsigContext.deviceId/userAgent，落库后在管理请求里原样回放。
        ("device_id", "TEXT NOT NULL DEFAULT ''"),
        ("user_agent", "TEXT NOT NULL DEFAULT ''"),
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
        ("trash_whitelist", "INTEGER NOT NULL DEFAULT 0"),
        ("tag_status", "TEXT NOT NULL DEFAULT 'active'"),
        ("tags", "TEXT NOT NULL DEFAULT '[]'"),
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
    # 兑换码：一个 (空间, 账号) 固定对应一个码，持码人可在公开兑换页
    # 反复导出该账号的加密 Sub2/CPA 凭证。空间删除时码随之失效。
    con.execute("""CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        workspace_master_id INTEGER NOT NULL,
        email TEXT NOT NULL COLLATE NOCASE,
        created_at REAL NOT NULL,
        redeem_count INTEGER NOT NULL DEFAULT 0,
        last_redeemed_at REAL,
        UNIQUE (workspace_master_id, email),
        FOREIGN KEY (workspace_master_id) REFERENCES workspace_masters(id) ON DELETE CASCADE
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_redeem_codes_email ON redeem_codes(email)")
    # allow_secret=1 时该码可在公开兑换页兑换明文 账号----密码----2FA；
    # 默认关闭，由「生成并导出兑换码」时的勾选控制。
    if "allow_secret" not in {
        r[1] for r in con.execute("PRAGMA table_info(redeem_codes)").fetchall()
    }:
        con.execute(
            "ALTER TABLE redeem_codes ADD COLUMN allow_secret INTEGER NOT NULL DEFAULT 0"
        )
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
    # Older connections did not enable SQLite foreign-key enforcement.  Purge
    # orphaned workspace rows once during migration so background workers can
    # no longer process candidates whose mother account was deleted.
    con.execute(
        "DELETE FROM workspace_credentials WHERE workspace_master_id NOT IN (SELECT id FROM workspace_masters)"
    )
    con.execute(
        "DELETE FROM workspace_candidates WHERE workspace_master_id NOT IN (SELECT id FROM workspace_masters)"
    )
    con.execute(
        "DELETE FROM redeem_codes WHERE workspace_master_id NOT IN (SELECT id FROM workspace_masters)"
    )
    # Plus 检测较早版本只把封号写进 extra_json.plus_check，未同步账号主状态。
    # 启动时补齐为统一的“已永久失效”类型，保证历史数据和新检测结果一致。
    con.execute(
        """
        UPDATE registered
         SET account_status='permanently_invalid'
         WHERE COALESCE(account_status, 'active') <> 'permanently_invalid'
           AND COALESCE(
               CASE WHEN json_valid(extra_json)
                    THEN json_extract(extra_json, '$.plus_check.status')
                    ELSE '' END,
               ''
           )='banned'
        """
    )
    con.execute(
        """
        UPDATE workspace_candidates
           SET status='permanently_invalid', updated_at=?
         WHERE email IN (
             SELECT email FROM registered
              WHERE account_status='permanently_invalid'
         )
           AND COALESCE(status, '') <> 'permanently_invalid'
        """,
        (time.time(),),
    )
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
    if "login_password" not in acc_cols:
        # 手动录入的 OpenAI 密码与 Outlook/Gmail 收件箱密码分开保存，避免
        # 在邮箱列表录入账号密码时破坏接码 provider 的登录凭证。
        con.execute("ALTER TABLE outlook_accounts ADD COLUMN login_password TEXT")
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
    if "register_mode" not in reg_cols:
        # 注册方式：protocol / camoufox / import；历史数据为空，前端显示「-」。
        con.execute("ALTER TABLE registered ADD COLUMN register_mode TEXT NOT NULL DEFAULT ''")
        con.commit()
    if "register_timezone" not in reg_cols:
        # Camoufox geoip 注册时浏览器实际生效的时区（IANA 名，如 America/New_York）。
        con.execute("ALTER TABLE registered ADD COLUMN register_timezone TEXT NOT NULL DEFAULT ''")
        con.commit()
    if "register_language" not in reg_cols:
        # Camoufox geoip 注册时浏览器实际生效的语言（navigator.language）。
        con.execute("ALTER TABLE registered ADD COLUMN register_language TEXT NOT NULL DEFAULT ''")
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

    # ── register_trace 历史回填 ──
    # 老库里的 registered 行补上注册方式/指纹/注册时间（registered_at 用
    # created_at 近似 —— 老数据没有更准的源）。两条语句幂等：每次都跑，
    # 第二条用 MAX 收敛到「runs 里能数出来的失败次数」，不会重复累计。
    con.execute(
        """INSERT OR IGNORE INTO register_trace
            (email, register_mode, register_timezone, register_language,
             registered_at, last_success_at, created_at, updated_at)
           SELECT email, register_mode, register_timezone, register_language,
                  created_at, created_at, created_at, created_at
             FROM registered WHERE email <> ''"""
    )
    # runs 表不区分注册 run 和仅登录 run，历史失败次数只能按 email 全量计入，
    # 会把历史登录失败也算进来 —— 接受这点偏差换取老账号的失败历史不清零。
    # 新失败一边自增 fail_count 一边落 runs 行，启动时 MAX 重算仍收敛一致。
    con.execute(
        """INSERT INTO register_trace
            (email, fail_count, last_fail_at, created_at, updated_at)
           SELECT email, COUNT(*), MAX(COALESCE(finished_at, started_at)),
                  MIN(started_at), MAX(COALESCE(finished_at, started_at))
             FROM runs WHERE status='failed' AND email <> '' GROUP BY email
           ON CONFLICT(email) DO UPDATE SET
              fail_count=MAX(register_trace.fail_count, excluded.fail_count),
              last_fail_at=MAX(COALESCE(register_trace.last_fail_at, 0),
                               excluded.last_fail_at)"""
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
        # 会话 JSON（/api/auth/session 导出）的 statsigContext 记录了登录时
        # 的设备指纹；也兼容顶层 device_id/userAgent 字段。
        statsig = metadata.get("statsigContext") if isinstance(metadata.get("statsigContext"), dict) else {}
        device_id = str(
            metadata.get("device_id") or metadata.get("deviceId")
            or metadata.get("oai_device_id") or statsig.get("deviceId") or ""
        ).strip()
        user_agent = str(
            metadata.get("user_agent") or metadata.get("userAgent")
            or statsig.get("userAgent") or ""
        ).strip()
        rows.append({"account": account[:255], "email": account[:255],
                     "workspace_id": workspace_id[:255], "access_token": access_token,
                     "session_token": session, "proxy_url": proxy,
                     "device_id": device_id[:255], "user_agent": user_agent[:500]})
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
                "SELECT id, account, email, workspace_id, access_token, session_token, proxy_url, device_id, user_agent FROM workspace_masters "
                "WHERE account=? OR session_token=? ORDER BY account=? DESC LIMIT 1",
                (item["account"], item["session_token"], item["account"]),
            ).fetchone()
            # 旧格式导入不带指纹时保留已有指纹；新导入带了就更新。
            item_device_id = item["device_id"] or (old["device_id"] if old else "")
            item_user_agent = item["user_agent"] or (old["user_agent"] if old else "")
            if old is None:
                con.execute(
                    "INSERT INTO workspace_masters"
                    "(account, email, workspace_id, access_token, session_token, proxy_url, device_id, user_agent, status, imported_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?, ?)",
                    (item["account"], item["email"], item["workspace_id"], item["access_token"], item["session_token"], item["proxy_url"], item_device_id, item_user_agent, now, now),
                )
                inserted += 1
            elif (
                old["account"] == item["account"]
                and old["email"] == item["email"]
                and old["workspace_id"] == item["workspace_id"]
                and old["access_token"] == item["access_token"]
                and old["session_token"] == item["session_token"]
                and old["proxy_url"] == item["proxy_url"]
                and old["device_id"] == item_device_id
                and old["user_agent"] == item_user_agent
            ):
                skipped += 1
            else:
                con.execute(
                    "UPDATE workspace_masters SET account=?, email=?, workspace_id=?, access_token=?, session_token=?, proxy_url=?, "
                    "device_id=?, user_agent=?, status='imported', updated_at=? WHERE id=?",
                    (item["account"], item["email"], item["workspace_id"], item["access_token"], item["session_token"], item["proxy_url"], item_device_id, item_user_agent, now, old["id"]),
                )
                updated += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def count_workspace_masters() -> int:
    return _conn().execute("SELECT COUNT(*) FROM workspace_masters").fetchone()[0]


def list_workspace_masters(limit: int = 20, offset: int = 0) -> list[dict]:
    rows = _conn().execute(
        "SELECT id, account, email, workspace_id, seats_in_use, seats_entitled, seats_default, seats_default_entitled, seats_usage_based, seats_prolite, seats_prolite_entitled, seats_default_available, seats_prolite_available, seats_default_held, seats_prolite_held, will_renew, is_delinquent, seat_cost, renewal_date, status, length(session_token) AS session_len, "
        "substr(session_token, 1, 8) AS session_head, "
        "substr(session_token, -6) AS session_tail, proxy_url, device_id, imported_at, updated_at "
        "FROM workspace_masters ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (max(1, min(int(limit), 200)), max(0, int(offset))),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["session_preview"] = f'{item.pop("session_head")}…{item.pop("session_tail")}'
        item["proxy_preview"] = _mask_proxy(item.pop("proxy_url", ""))
        item["has_device_fingerprint"] = bool((item.pop("device_id", "") or "").strip())
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
    # 候选人专属代理池：为空时回退到 proxy_pool（即前端同步下来的全局池），
    # 非空时额度查询/401 重登录/凭证获取都改走它，与全局池解绑。
    "quota_proxy_pool": "",
    # 暂停本空间全部自动化任务：定时额度、席位补齐、垃圾箱自动回收。
    "automation_paused": False,
    "auto_push": False,
    # 空间专属的号池推送目标开关：勾选才推送对应目标，可两个都开。
    "auto_push_sub2api_enabled": True,
    "auto_push_cpa_enabled": True,
    # 空间专属的 Sub2API 号池推送配置；每项留空都跟随全局导出配置。
    "auto_push_sub2api_url": "",
    "auto_push_sub2api_api_key": "",
    "auto_push_sub2api_group_ids": "",
    # 空间专属的 CPA 推送配置；每项留空都跟随全局导出配置。
    "auto_push_cpa_url": "",
    "auto_push_cpa_mgmt_key": "",
    # 推送进 CPA 的账号优先级（凭证 JSON 的 priority 字段），可为负数，默认 0。
    "auto_push_cpa_priority": 0,
    # CPA 静态家宽代理池：默认关闭；启用后推送 CPA（含手动推送）时给凭证
    # JSON 写 proxy_url，按租用计数最少取用；与全局/空间候选人代理池完全独立。
    "cpa_static_proxy_enabled": False,
    "cpa_static_proxy_pool": "",
    # Codex/Usage-based 席位是否跳过自动推送。只影响自动流程，手动推送不受此限。
    "auto_push_skip_codex_seat": True,
    "concurrency": 1,
    "otp_timeout": 180,
    "account_retry_count": 1,
    "cool_down_seconds": 0,
    "quota_enabled": False,
    "quota_network_retries": 2,
    # 额度耗尽时自动兑换一张重置券再决定要不要入箱。默认关闭：券是不可逆的
    # 消耗品，兑掉就没了，必须由用户显式打开。
    "quota_auto_reset_enabled": False,
    "trash_enabled": True,
    "trash_invalid_enabled": True,
    # 入箱前置动作：seat = 席位切换为 Codex（成员留在空间）；kick = 直接从
    # 空间踢出成员（有的母号空间不支持/不适合切 Codex，只能踢人）。
    "trash_action": "seat",
    "trash_zero_delay_minutes": 1,
    # 额度耗尽判定看哪个限流窗口：any / five_hour / weekly，默认仅看周限制。
    # 上游 wham/usage 的 primary/secondary 并不固定对应 5h/周（多数账号只有
    # 周窗口且落在 primary），所以窗口按 window_seconds 认，不按字段名认。
    "trash_zero_quota_window": "weekly",
    # 同一轮回收里连续入箱时，两次入箱之间的等待秒数（串行限速）。
    "trash_gap_seconds": 30,
    "seat_protect_enabled": False,
    "seat_protect_threshold": 8,
    "seat_protect_refresh_time": "00:00",
    "seat_protect_used_count": 0,
    "seat_protect_window_key": "",
    "prolite_seat_protect_enabled": False,
    "prolite_seat_protect_threshold": 8,
    "prolite_seat_protect_refresh_time": "00:00",
    "prolite_seat_protect_used_count": 0,
    "prolite_seat_protect_window_key": "",
    "auto_standard_seat_enabled": False,
    "auto_prolite_seat_enabled": False,
    # 补齐目标席位数：0 表示跟随已购席位上限（entitled），正数则作为固定目标，
    # 生效时仍会被钳制到不超过已购上限。
    "auto_standard_seat_target": 0,
    "auto_prolite_seat_target": 0,
    "auto_seat_interval_minutes": 5,
    # 席位补齐是串行的：每切换一个成员后等待这么多秒再切下一个。
    "auto_seat_switch_gap_seconds": 30,
    "auto_prolite_candidate_seat_type": "default",
    # 补齐来源：switch=仅切换已加入成员；invite=仅邀请划分到空间但尚未受邀的
    # 候选人（直接邀请到目标席位）；mixed=先切换已加入成员，不足时再邀请。
    "auto_standard_seat_source": "switch",
    "auto_prolite_seat_source": "switch",
    # 批量踢出空间成员：每踢完一个在 [min, max] 秒内随机 sleep，再踢下一个；
    # 两个都填 0 表示不等待。
    "kick_delay_min_seconds": 2,
    "kick_delay_max_seconds": 5,
    "standard_fulfilled_total": 0,
    "prolite_fulfilled_total": 0,
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


def normalize_gap_seconds(value: object, default: int, maximum: int = 600) -> int:
    """把串行节奏类设置钳到 [0, maximum]。

    不能写成 ``int(value or default)``：0 是合法取值（不等待），会被 ``or`` 吃掉
    还原成默认值。设置默认值住在本模块，所以归一化也放这里，供 app 层复用。
    """
    if value is None or value == "":
        return default
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(maximum, seconds))


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
    if values.get("prolite_seat_protect_enabled"):
        refresh_time = _normalize_hhmm(values.get("prolite_seat_protect_refresh_time") or "00:00")
        current_key = _workspace_seat_protect_window_key(time.time(), refresh_time)
        stored_key = str(values.get("prolite_seat_protect_window_key") or "").strip()
        if stored_key != current_key:
            values["prolite_seat_protect_window_key"] = current_key
            values["prolite_seat_protect_used_count"] = 0
            update_workspace_settings(int(workspace_id), {
                "prolite_seat_protect_window_key": current_key,
                "prolite_seat_protect_used_count": 0,
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


def reserve_workspace_prolite_seat_protect_quota(workspace_id: int, amount: int = 1) -> dict:
    """Reserve one or more advanced (ProLite) seat-switch operations."""
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
        enabled = bool(settings.get("prolite_seat_protect_enabled", False))
        threshold = int(settings.get("prolite_seat_protect_threshold") or 8)
        refresh_time = _normalize_hhmm(settings.get("prolite_seat_protect_refresh_time") or "00:00")
        window_key = _workspace_seat_protect_window_key(time.time(), refresh_time)
        stored_window = str(settings.get("prolite_seat_protect_window_key") or "").strip()
        used = int(settings.get("prolite_seat_protect_used_count") or 0)
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
            settings["prolite_seat_protect_enabled"] = enabled
            settings["prolite_seat_protect_threshold"] = threshold
            settings["prolite_seat_protect_refresh_time"] = refresh_time
            settings["prolite_seat_protect_window_key"] = window_key
            settings["prolite_seat_protect_used_count"] = used + amount
            con.execute(
                "UPDATE workspace_masters SET settings_json=?, updated_at=? WHERE id=?",
                (json.dumps(settings, ensure_ascii=False), time.time(), int(workspace_id)),
            )
            con.commit()
            result["used_count"] = used + amount
        return result


def release_workspace_prolite_seat_protect_quota(workspace_id: int, amount: int = 1) -> bool:
    """Release a previously reserved advanced seat protection quota."""
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
        if not bool(settings.get("prolite_seat_protect_enabled", False)):
            return False
        refresh_time = _normalize_hhmm(settings.get("prolite_seat_protect_refresh_time") or "00:00")
        window_key = _workspace_seat_protect_window_key(time.time(), refresh_time)
        stored_window = str(settings.get("prolite_seat_protect_window_key") or "").strip()
        used = int(settings.get("prolite_seat_protect_used_count") or 0)
        if stored_window != window_key:
            used = 0
        settings["prolite_seat_protect_window_key"] = window_key
        settings["prolite_seat_protect_used_count"] = max(0, used - amount)
        con.execute(
            "UPDATE workspace_masters SET settings_json=?, updated_at=? WHERE id=?",
            (json.dumps(settings, ensure_ascii=False), time.time(), int(workspace_id)),
        )
        con.commit()
        return True


def increment_workspace_fulfillment_counter(workspace_id: int, seat_type: str, amount: int = 1) -> int:
    """递增指定母号空间的席位补齐历史累计计数，并返回递增后的总数。"""
    amount = max(1, int(amount or 1))
    canonical_seat = _canonical_workspace_seat_type(seat_type)
    counter_key = "prolite_fulfilled_total" if canonical_seat == "prolite" else "standard_fulfilled_total"
    with _lock:
        con = _conn()
        row = con.execute("SELECT settings_json FROM workspace_masters WHERE id=?", (int(workspace_id),)).fetchone()
        if not row:
            return 0
        try:
            raw = json.loads(row["settings_json"] or "{}")
            settings = raw if isinstance(raw, dict) else {}
        except Exception:
            settings = {}
        current = max(0, int(settings.get(counter_key) or 0))
        new_total = current + amount
        settings[counter_key] = new_total
        con.execute(
            "UPDATE workspace_masters SET settings_json=?, updated_at=? WHERE id=?",
            (json.dumps(settings, ensure_ascii=False), time.time(), int(workspace_id)),
        )
        con.commit()
        return new_total


def delete_workspace_master(workspace_id: int) -> bool:
    with _lock:
        con = _conn()
        # Keep deletion safe even for databases created before PRAGMA
        # foreign_keys=ON was enabled; explicitly remove child rows first.
        con.execute(
            "DELETE FROM workspace_credentials WHERE workspace_master_id=?",
            (int(workspace_id),),
        )
        con.execute(
            "DELETE FROM workspace_candidates WHERE workspace_master_id=?",
            (int(workspace_id),),
        )
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


def update_workspace_master_auth(
    workspace_id: int,
    access_token: str,
    session_token: str | None = None,
) -> bool:
    """更新母号管理凭证（通常由 session token 刷新得到）。"""
    access = str(access_token or "").strip()
    if not access:
        return False
    with _lock:
        con = _conn()
        if session_token is None:
            rc = con.execute(
                "UPDATE workspace_masters SET access_token=?, updated_at=? WHERE id=?",
                (access, time.time(), int(workspace_id)),
            )
        else:
            session = str(session_token or "").strip()
            rc = con.execute(
                "UPDATE workspace_masters SET access_token=?, session_token=?, updated_at=? WHERE id=?",
                (access, session, time.time(), int(workspace_id)),
            )
        con.commit()
        return rc.rowcount > 0


def delete_workspace_masters(ids: list[int]) -> int:
    cleaned = sorted({int(i) for i in (ids or []) if int(i) > 0})
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        marks = ",".join("?" * len(cleaned))
        con.execute(
            f"DELETE FROM workspace_credentials WHERE workspace_master_id IN ({marks})",
            cleaned,
        )
        con.execute(
            f"DELETE FROM workspace_candidates WHERE workspace_master_id IN ({marks})",
            cleaned,
        )
        rc = con.execute(
            f"DELETE FROM workspace_masters WHERE id IN ({marks})", cleaned
        )
        con.commit()
        return rc.rowcount


def list_workspace_candidates(workspace_master_id: int = 0) -> list[dict]:
    join_status_expr = _workspace_candidate_join_status_expr()
    sql = """SELECT c.workspace_master_id, c.email, c.status,
                     COALESCE(c.trash_status, 'active') AS trash_status,
                     COALESCE(c.trash_due_at, 0) AS trash_due_at,
                     COALESCE(c.trash_reason, '') AS trash_reason,
                     COALESCE(c.trash_whitelist, 0) AS trash_whitelist,
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
    group_name: str = "",
    tag: str = "",
    redeem_status: str = "",
    quota_status: str = "",
    keyword: str = "",
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
        clauses.append("length(COALESCE(wc.access_token,''))>0")
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
        elif normalized == "prolite":
            clauses.append(
                "LOWER(COALESCE(c.seat_type,'')) IN ('prolite', 'pro_lite', 'advanced', 'advanced_seat', 'premium', 'premium_seat', 'pro', '高级', '高级席位')"
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
    if group_name:
        clauses.append("r.group_name=?")
        args.append(str(group_name))
    tag_label = str(tag or "").strip()
    if tag_label:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(COALESCE(c.tags,'[]')) WHERE value=?)"
        )
        args.append(tag_label)
    normalized_redeem = str(redeem_status or "").strip().lower()
    if normalized_redeem:
        if normalized_redeem not in {"has_code", "no_code"}:
            raise ValueError("redeem_status 只能是 has_code / no_code")
        clauses.append(
            ("EXISTS" if normalized_redeem == "has_code" else "NOT EXISTS")
            + " (SELECT 1 FROM redeem_codes rc"
              " WHERE rc.workspace_master_id=c.workspace_master_id AND rc.email=c.email)"
        )
    normalized_quota = str(quota_status or "").strip().lower()
    if normalized_quota:
        if normalized_quota != "zero":
            raise ValueError("quota_status 只能是 zero")
        # 与 app._is_zero_quota_payload 的「any」口径一致：任一窗口
        # used_percent>=100（剩余 0%）或 credits_balance<=0；查询失败
        # （带 error_code）和没查过额度的行都不算耗尽。
        clauses.append(
            "json_valid(COALESCE(wc.quota_json,''))"
            " AND json_extract(wc.quota_json,'$.error_code') IS NULL"
            " AND (CAST(COALESCE(json_extract(wc.quota_json,'$.primary.used_percent'),0) AS REAL)>=100"
            "  OR CAST(COALESCE(json_extract(wc.quota_json,'$.secondary.used_percent'),0) AS REAL)>=100"
            "  OR (json_extract(wc.quota_json,'$.credits_balance') IS NOT NULL"
            "      AND CAST(json_extract(wc.quota_json,'$.credits_balance') AS REAL)<=0))"
        )
    kw = str(keyword or "").strip()
    if kw:
        like = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append(
            "(r.email LIKE ? ESCAPE '\\' OR r.group_name LIKE ? ESCAPE '\\')"
        )
        args.extend([like, like])
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
    group_name: str = "",
    tag: str = "",
    redeem_status: str = "",
    quota_status: str = "",
    keyword: str = "",
) -> list[dict]:
    where, args = _workspace_candidate_option_filters(
        workspace_master_id, account_status, join_status, credential_status, seat_type, trash_status, tag_status, group_name, tag, redeem_status, quota_status, keyword,
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
        COALESCE(c.trash_whitelist, 0) AS trash_whitelist,
        COALESCE(c.tag_status, 'active') AS tag_status,
        COALESCE(c.tags, '[]') AS tags,
        """ + join_status_expr + """ AS workspace_join_status,
        wc.quota_json,
        r.account_status,
        CASE WHEN COALESCE(r.account_status, 'active') <> 'permanently_invalid' AND length(r.access_token)>0 THEN 1 ELSE 0 END AS has_access_token,
        CASE WHEN COALESCE(r.account_status, 'active') <> 'permanently_invalid' AND length(COALESCE(wc.access_token,''))>0 THEN 1 ELSE 0 END AS has_workspace_access_token,
        CASE WHEN COALESCE(c.trash_status, 'active')='trashed' THEN 'trashed'
             WHEN COALESCE(c.trash_status, 'active')='scheduled' THEN 'trash_scheduled'
             WHEN COALESCE(r.account_status, 'active')='permanently_invalid' THEN 'unavailable'
             WHEN length(COALESCE(wc.access_token,''))>0 THEN 'workspace_credential'
             WHEN length(COALESCE(r.access_token,''))>0 THEN 'personal_credential'
             ELSE 'none' END AS credential_status,
        CASE WHEN COALESCE(c.trash_status, 'active')='trashed' THEN 'trashed'
             WHEN COALESCE(c.trash_status, 'active')='scheduled' THEN 'trash_scheduled'
             WHEN COALESCE(r.account_status, 'active')='permanently_invalid' THEN 'unavailable'
             WHEN c.status LIKE 'quota_error_%' THEN c.status
             WHEN length(COALESCE(wc.access_token,''))>0 THEN 'workspace_credential'
             ELSE CASE WHEN """ + join_status_expr + """ = 'joined' THEN 'joined' ELSE c.workspace_join_status END END AS display_status,
        rc.code AS redeem_code,
        CASE WHEN rc.code IS NULL THEN 0 ELSE 1 END AS has_redeem_code,
        COALESCE(pl.proxy, '') AS cpa_proxy,
        COALESCE(rt.registered_at, r.created_at) AS registered_at,
        1 AS assigned
        FROM registered r JOIN workspace_candidates c
          ON c.email=r.email
        LEFT JOIN workspace_credentials wc ON wc.email=r.email AND wc.workspace_master_id=?
        LEFT JOIN redeem_codes rc ON rc.workspace_master_id=c.workspace_master_id AND rc.email=c.email
        LEFT JOIN cpa_proxy_leases pl ON pl.workspace_master_id=c.workspace_master_id AND pl.email=c.email
        LEFT JOIN register_trace rt ON rt.email=r.email
        WHERE """ + where.replace("c.workspace_master_id=?", "c.workspace_master_id=?") + " ORDER BY r.created_at DESC"
    # workspace id 同时用于 JOIN 左表和过滤条件。
    query_args = [int(workspace_master_id), *args]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        query_args.extend([max(1, int(limit)), max(0, int(offset))])
    rows = _conn().execute(sql, query_args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = _parse_candidate_tags(d.get("tags"))
        out.append(d)
    return out


def count_workspace_candidate_options(
    workspace_master_id: int,
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
) -> int:
    where, args = _workspace_candidate_option_filters(
        workspace_master_id, account_status, join_status, credential_status, seat_type, trash_status, tag_status, group_name, tag, redeem_status, quota_status, keyword,
    )
    return int(_conn().execute("""SELECT COUNT(*) FROM registered r
        JOIN workspace_candidates c ON c.email=r.email
        LEFT JOIN workspace_credentials wc ON wc.email=r.email AND wc.workspace_master_id=?
        WHERE """ + where, [int(workspace_master_id), *args]).fetchone()[0])


def _parse_candidate_tags(raw) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(t) for t in value if str(t).strip()]


def _normalize_candidate_tags(tags) -> list[str]:
    seen: dict[str, None] = {}
    for t in tags or []:
        label = str(t or "").strip()[:32]
        if label and label not in seen:
            seen[label] = None
        if len(seen) >= 20:
            break
    return list(seen)


def list_workspace_candidate_tags(workspace_master_id: int) -> list[str]:
    """该空间全部候选人上不重复的标签列表（供筛选下拉）。"""
    rows = _conn().execute(
        "SELECT tags FROM workspace_candidates WHERE workspace_master_id=?",
        (int(workspace_master_id),),
    ).fetchall()
    seen: dict[str, None] = {}
    for r in rows:
        for label in _parse_candidate_tags(r[0]):
            seen.setdefault(label, None)
    return sorted(seen)


def set_workspace_candidate_tags(
    workspace_master_id: int,
    emails: list[str],
    tags,
    mode: str = "add",
) -> int:
    """对所选候选人批量打标。mode: add 追加 / remove 移除 / set 覆盖。"""
    if mode not in {"add", "remove", "set"}:
        raise ValueError("mode 只能是 add / remove / set")
    new_tags = _normalize_candidate_tags(tags)
    cleaned = [str(e or "").strip().lower() for e in emails if str(e or "").strip()]
    if not cleaned:
        return 0
    marks = ",".join("?" * len(cleaned))
    now = time.time()
    changed = 0
    with _lock:
        con = _conn()
        cur = con.execute(
            f"SELECT email, tags FROM workspace_candidates "
            f"WHERE workspace_master_id=? AND email IN ({marks})",
            [int(workspace_master_id), *cleaned],
        )
        for row in cur.fetchall():
            current = _parse_candidate_tags(row["tags"])
            if mode == "set":
                merged = new_tags
            elif mode == "add":
                merged = current + [t for t in new_tags if t not in current]
            else:
                drop = set(new_tags)
                merged = [t for t in current if t not in drop]
            if merged == current:
                continue
            rc = con.execute(
                "UPDATE workspace_candidates SET tags=?, updated_at=? "
                "WHERE workspace_master_id=? AND email=?",
                (json.dumps(merged, ensure_ascii=False), now,
                 int(workspace_master_id), row["email"]),
            )
            changed += rc.rowcount
        con.commit()
    return changed


def get_workspace_candidate_stats(workspace_master_id: int) -> dict:
    """返回指定母号空间的候选人状态统计，包括垃圾回收与席位保护补齐相关统计。"""
    wid = int(workspace_master_id)
    con = _conn()
    now = time.time()

    # 候选表总数与垃圾箱状态统计
    # 关联 registered，以准确识别 account_status
    rows = con.execute("""
        SELECT
            COUNT(*) AS total_candidates,
            SUM(CASE WHEN COALESCE(c.trash_status, 'active') = 'trashed' THEN 1 ELSE 0 END) AS trashed_count,
            SUM(CASE WHEN COALESCE(c.trash_status, 'active') = 'scheduled' THEN 1 ELSE 0 END) AS scheduled_trash_count,
            SUM(CASE WHEN COALESCE(c.trash_status, 'active') = 'scheduled' AND COALESCE(c.trash_due_at, 0) > 0 AND c.trash_due_at <= ? THEN 1 ELSE 0 END) AS due_scheduled_trash_count,
            SUM(CASE WHEN COALESCE(r.account_status, 'active') = 'permanently_invalid' AND COALESCE(c.trash_status, 'active') <> 'trashed' THEN 1 ELSE 0 END) AS invalid_pending_trash_count,
            SUM(CASE WHEN COALESCE(r.account_status, 'active') = 'permanently_invalid' THEN 1 ELSE 0 END) AS invalid_total_count,
            SUM(CASE WHEN COALESCE(c.tag_status, 'active') = 'outbound' THEN 1 ELSE 0 END) AS outbound_count,
            SUM(CASE WHEN COALESCE(c.seat_type, '') IN ('default', 'standard', 'standard_seat') OR (COALESCE(c.seat_type, '') = '' AND COALESCE(c.gpt_seat, '') <> '') THEN 1 ELSE 0 END) AS standard_seat_count,
            SUM(CASE WHEN COALESCE(c.seat_type, '') IN ('prolite', 'pro_lite', 'advanced', 'premium') THEN 1 ELSE 0 END) AS prolite_seat_count,
            SUM(CASE WHEN COALESCE(c.seat_type, '') IN ('usage_based', 'codex') OR (COALESCE(c.seat_type, '') = '' AND COALESCE(c.codex_seat, '') <> '') THEN 1 ELSE 0 END) AS codex_seat_count
        FROM workspace_candidates c
        JOIN registered r ON r.email = c.email
        WHERE c.workspace_master_id = ?
    """, (now, wid)).fetchone()

    data = dict(rows) if rows else {}
    settings = get_workspace_settings(wid)

    return {
        "workspace_id": wid,
        "total_candidates": int(data.get("total_candidates") or 0),
        "trash": {
            "trashed_count": int(data.get("trashed_count") or 0),
            "scheduled_count": int(data.get("scheduled_count") or data.get("scheduled_trash_count") or 0),
            "due_scheduled_count": int(data.get("due_scheduled_trash_count") or 0),
            "invalid_pending_trash_count": int(data.get("invalid_pending_trash_count") or 0),
            "invalid_total_count": int(data.get("invalid_total_count") or 0),
            "trash_enabled": bool(settings.get("trash_enabled", True)),
            "trash_invalid_enabled": bool(settings.get("trash_invalid_enabled", True)),
            "trash_action": str(settings.get("trash_action") or "seat"),
            "trash_zero_delay_minutes": int(settings.get("trash_zero_delay_minutes") or 1),
            "trash_zero_quota_window": str(settings.get("trash_zero_quota_window") or "weekly"),
            "trash_gap_seconds": normalize_gap_seconds(settings.get("trash_gap_seconds"), 30),
        },
        "seat_fulfillment": {
            "standard": {
                "count": int(data.get("standard_seat_count") or 0),
                "fulfilled_total": int(settings.get("standard_fulfilled_total") or 0),
                "auto_enabled": bool(settings.get("auto_standard_seat_enabled", False)),
                "protect_enabled": bool(settings.get("seat_protect_enabled", False)),
                "protect_used_count": int(settings.get("seat_protect_used_count") or 0),
                "protect_threshold": int(settings.get("seat_protect_threshold") or 8),
                "protect_refresh_time": str(settings.get("seat_protect_refresh_time") or "00:00"),
                "protect_window_key": str(settings.get("seat_protect_window_key") or ""),
                "target": int(settings.get("auto_standard_seat_target") or 0),
                "source": str(settings.get("auto_standard_seat_source") or "switch"),
            },
            "prolite": {
                "count": int(data.get("prolite_seat_count") or 0),
                "fulfilled_total": int(settings.get("prolite_fulfilled_total") or 0),
                "auto_enabled": bool(settings.get("auto_prolite_seat_enabled", False)),
                "protect_enabled": bool(settings.get("prolite_seat_protect_enabled", False)),
                "protect_used_count": int(settings.get("prolite_seat_protect_used_count") or 0),
                "protect_threshold": int(settings.get("prolite_seat_protect_threshold") or 8),
                "protect_refresh_time": str(settings.get("prolite_seat_protect_refresh_time") or "00:00"),
                "protect_window_key": str(settings.get("prolite_seat_protect_window_key") or ""),
                "target": int(settings.get("auto_prolite_seat_target") or 0),
                "source": str(settings.get("auto_prolite_seat_source") or "switch"),
            },
            "codex": {
                "count": int(data.get("codex_seat_count") or 0),
            },
            "outbound_count": int(data.get("outbound_count") or 0),
            "auto_interval_minutes": int(settings.get("auto_seat_interval_minutes") or 5),
            "auto_switch_gap_seconds": normalize_gap_seconds(settings.get("auto_seat_switch_gap_seconds"), 30),
            "auto_prolite_candidate_seat_type": str(settings.get("auto_prolite_candidate_seat_type") or "default"),
        }
    }


def list_workspace_candidate_groups(workspace_master_id: int) -> list[str]:
    """返回指定空间候选人中出现的所有分组名（去重、按名排序）。"""
    rows = _conn().execute(
        """SELECT DISTINCT r.group_name
           FROM registered r
           JOIN workspace_candidates c ON c.email=r.email
           WHERE c.workspace_master_id=? AND COALESCE(r.group_name, '') <> ''
           ORDER BY r.group_name COLLATE NOCASE""",
        (int(workspace_master_id),),
    ).fetchall()
    return [str(row[0]) for row in rows]


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
           AND COALESCE(c.trash_whitelist, 0) = 0
         ORDER BY c.trash_due_at ASC, c.updated_at ASC
        """,
        (now,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_invalid_workspace_candidates_pending_trash(limit: int = 500) -> list[dict]:
    rows = _conn().execute(
        """
        SELECT c.*
          FROM workspace_candidates c
          JOIN registered r ON r.email=c.email
          JOIN workspace_masters m ON m.id=c.workspace_master_id
         WHERE r.account_status='permanently_invalid'
           AND COALESCE(c.trash_status, 'active')<>'trashed'
           AND COALESCE(c.trash_whitelist, 0) = 0
         ORDER BY c.updated_at ASC
         LIMIT ?
        """,
        (max(1, min(5000, int(limit or 500))),),
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


def list_trashed_workspace_candidate_emails(workspace_master_id: int) -> list[str]:
    """返回指定空间垃圾箱内全部候选人的邮箱。"""
    rows = _conn().execute(
        "SELECT email FROM workspace_candidates "
        "WHERE workspace_master_id=? AND COALESCE(trash_status, 'active')='trashed'",
        (int(workspace_master_id),),
    ).fetchall()
    return [str(r["email"]) for r in rows]


def restore_workspace_candidates_from_trash(
    workspace_master_id: int,
    emails: list[str],
) -> int:
    """Restore trashed candidate relationships without changing their seat or join state.

    手动移出垃圾箱的账号自动加入垃圾箱白名单：防止刚恢复就被下一轮
    自动回收扫回去；白名单可在候选管理里手动关掉。
    """
    cleaned = sorted({str(email or "").strip().lower() for email in emails if str(email or "").strip()})
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        marks = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"""
            UPDATE workspace_candidates
               SET trash_status='active', trash_due_at=0, trash_reason='',
                   trash_whitelist=1, updated_at=?
             WHERE workspace_master_id=?
               AND email IN ({marks})
               AND trash_status='trashed'
            """,
            [time.time(), int(workspace_master_id), *cleaned],
        )
        con.commit()
        return rc.rowcount


def set_workspace_candidates_trash_whitelist(
    workspace_master_id: int,
    emails: list[str],
    *,
    enabled: bool,
) -> int:
    """设置垃圾箱白名单开关，返回实际更新的行数。

    开启时顺带撤掉这些账号挂起的入箱排期（scheduled→active、due_at 清零）；
    已入箱的行保持 trashed 不动——白名单防的是未来的自动回收，不替代恢复操作。
    """
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not cleaned or not int(workspace_master_id or 0):
        return 0
    marks = ",".join("?" * len(cleaned))
    now = time.time()
    with _lock:
        con = _conn()
        if enabled:
            rc = con.execute(
                f"""
                UPDATE workspace_candidates
                   SET trash_whitelist=1,
                       trash_status=CASE WHEN trash_status='scheduled' THEN 'active' ELSE trash_status END,
                       trash_due_at=CASE WHEN trash_status='scheduled' THEN 0 ELSE trash_due_at END,
                       updated_at=?
                 WHERE workspace_master_id=? AND email IN ({marks})
                """,
                [now, int(workspace_master_id), *cleaned],
            )
        else:
            rc = con.execute(
                f"""
                UPDATE workspace_candidates
                   SET trash_whitelist=0, updated_at=?
                 WHERE workspace_master_id=? AND email IN ({marks})
                """,
                [now, int(workspace_master_id), *cleaned],
            )
        con.commit()
        return rc.rowcount


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


REDEEM_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
REDEEM_CODE_LENGTH = 12


def _generate_redeem_code() -> str:
    return "".join(secrets.choice(REDEEM_CODE_ALPHABET) for _ in range(REDEEM_CODE_LENGTH))


def normalize_redeem_code(code: object) -> str:
    return str(code or "").strip().upper()


def get_or_create_redeem_codes(
    workspace_master_id: int, emails: list[str], allow_secret: bool = False
) -> dict[str, str]:
    """返回 {email: code}。同一 (空间, 账号) 永远复用同一个码，重复导出幂等。

    ``allow_secret`` 是生成时的权威勾选：重复导出同一批账号会把这些码的
    密码/2FA 兑换权限同步为本次勾选值（勾选开启、不勾关闭）。
    """
    wid = int(workspace_master_id or 0)
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not wid or not cleaned:
        return {}
    marks = ",".join("?" * len(cleaned))
    with _lock:
        con = _conn()
        rows = con.execute(
            f"SELECT email, code FROM redeem_codes WHERE workspace_master_id=? AND email IN ({marks})",
            [wid, *cleaned],
        ).fetchall()
        out = {str(r["email"]).lower(): str(r["code"]) for r in rows}
        now = time.time()
        for email in cleaned:
            if email in out:
                continue
            for _ in range(20):
                code = _generate_redeem_code()
                try:
                    con.execute(
                        "INSERT INTO redeem_codes(code,workspace_master_id,email,created_at) VALUES (?,?,?,?)",
                        (code, wid, email, now),
                    )
                    out[email] = code
                    break
                except sqlite3.IntegrityError:
                    continue
        con.execute(
            f"UPDATE redeem_codes SET allow_secret=? WHERE workspace_master_id=? AND email IN ({marks})",
            (1 if allow_secret else 0, wid, *cleaned),
        )
        con.commit()
    return out


def get_redeem_code(code: object) -> Optional[dict]:
    key = normalize_redeem_code(code)
    if not key:
        return None
    row = _conn().execute(
        """SELECT rc.code, rc.workspace_master_id, rc.email, rc.created_at,
                  rc.redeem_count, rc.last_redeemed_at, rc.allow_secret,
                  m.account AS master_account, m.workspace_id AS workspace_id
           FROM redeem_codes rc
           LEFT JOIN workspace_masters m ON m.id=rc.workspace_master_id
           WHERE rc.code=?""",
        (key,),
    ).fetchone()
    return dict(row) if row else None


def list_redeem_codes(workspace_master_id: int = 0) -> list[dict]:
    where = ""
    args: list = []
    if workspace_master_id:
        where = "WHERE rc.workspace_master_id=?"
        args.append(int(workspace_master_id))
    rows = _conn().execute(
        """SELECT rc.code, rc.workspace_master_id, rc.email, rc.created_at,
                  rc.redeem_count, rc.last_redeemed_at, rc.allow_secret,
                  m.account AS master_account, m.workspace_id AS workspace_id,
                  CASE WHEN length(COALESCE(wc.access_token,''))>0 THEN 1 ELSE 0 END AS has_credential
           FROM redeem_codes rc
           LEFT JOIN workspace_masters m ON m.id=rc.workspace_master_id
           LEFT JOIN workspace_credentials wc
             ON wc.workspace_master_id=rc.workspace_master_id AND wc.email=rc.email
           """ + where + " ORDER BY rc.created_at DESC",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def delete_redeem_codes(codes: list[str]) -> int:
    cleaned = sorted({normalize_redeem_code(c) for c in (codes or []) if normalize_redeem_code(c)})
    if not cleaned:
        return 0
    marks = ",".join("?" * len(cleaned))
    with _lock:
        con = _conn()
        cur = con.execute(f"DELETE FROM redeem_codes WHERE code IN ({marks})", cleaned)
        con.commit()
        return int(cur.rowcount or 0)


def mark_redeem_code_used(code: object) -> None:
    key = normalize_redeem_code(code)
    if not key:
        return
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE redeem_codes SET redeem_count=redeem_count+1, last_redeemed_at=? WHERE code=?",
            (time.time(), key),
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


def remove_workspace_assignment(workspace_master_id: int, emails: list[str]) -> dict:
    """彻底解除空间划分：候选行 + 该空间已获取凭证一起删（一个事务）。

    踢出空间成员后使用 —— 人已经不在空间里，留着空间凭证只会变成孤儿数据。
    注册结果和号池不受影响，账号还能重新划分到其他空间。
    """
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    counts = {"candidates": 0, "credentials": 0}
    if not cleaned:
        return counts
    marks = ",".join("?" * len(cleaned))
    with _lock:
        con = _conn()
        rc = con.execute(
            f"DELETE FROM workspace_candidates WHERE workspace_master_id=? AND email IN ({marks})",
            [int(workspace_master_id), *cleaned],
        )
        counts["candidates"] = rc.rowcount
        rc = con.execute(
            f"DELETE FROM workspace_credentials WHERE workspace_master_id=? AND email IN ({marks})",
            [int(workspace_master_id), *cleaned],
        )
        counts["credentials"] = rc.rowcount
        con.commit()
    return counts


def mark_workspace_candidates_kicked(
    workspace_master_id: int,
    emails: list[str],
    reason: str = "kicked",
) -> dict:
    """踢出空间成员后的本地落库：候选行保留但移入本空间垃圾箱（trash_reason 默认 'kicked'）。

    一个事务里完成：清空 member_id/席位快照、加入状态回到 not_invited、
    trash_status='trashed'，并删掉该空间凭证（人已不在空间，凭证即失效）。
    注册结果/号池不受影响；从垃圾箱恢复后仍是普通候选人，可再次邀请。
    ``reason``：踢出模式下的自动入箱会传入真实触发原因（如 quota_zero），
    保留"为什么入箱"的语义而不是一律记成 kicked。
    """
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    counts = {"candidates": 0, "credentials": 0}
    if not cleaned:
        return counts
    marks = ",".join("?" * len(cleaned))
    now = time.time()
    reason = str(reason or "kicked")[:500]
    with _lock:
        con = _conn()
        rc = con.execute(
            f"""
            UPDATE workspace_candidates
               SET member_id='', seat_type='', codex_seat='', gpt_seat='',
                   workspace_join_status='not_invited',
                   trash_status='trashed', trash_due_at=0, trash_reason=?,
                   updated_at=?
             WHERE workspace_master_id=? AND email IN ({marks})
            """,
            [reason, now, int(workspace_master_id), *cleaned],
        )
        counts["candidates"] = rc.rowcount
        rc = con.execute(
            f"DELETE FROM workspace_credentials WHERE workspace_master_id=? AND email IN ({marks})",
            [int(workspace_master_id), *cleaned],
        )
        counts["credentials"] = rc.rowcount
        con.commit()
    return counts


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
    allowed = {k: values[k] for k in ('seats_in_use','seats_entitled','seats_default','seats_default_entitled','seats_usage_based','seats_prolite','seats_prolite_entitled','seats_default_available','seats_prolite_available','seats_default_held','seats_prolite_held','will_renew','is_delinquent','seat_cost','renewal_date') if k in values}
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
    relay_suffix: str = "",
) -> dict:
    """批量入库。已存在的 email 仅在凭证变化时更新。

    解析阶段全对才写：有一行非法就整批拒绝（抛 ImportValidationError），
    不会出现"写进去一半"对不上账的情况。

    ``relay_suffix``：自定义追加串（如 ``?json=1``），逐行拼到 OTP 中转
    链接尾部，让中转服务按 JSON 等格式返回。
    """
    relay_suffix = str(relay_suffix or "")
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
            if relay and relay_suffix:
                relay += relay_suffix

            cur = con.execute(
                "SELECT password, refresh_token, relay_url, kind, group_name "
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
                (existing["password"] or "") != password
                or
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


def update_account_password(email: str, password: str) -> str | None:
    """手动录入邮箱列表账号的 OpenAI 密码。

    已有注册结果时，密码应写入 ``registered``（那里才是 OpenAI 登录密码）；
    尚未产生注册结果的账号则写回号池行的 ``login_password``。该列与邮箱
    provider 的收件箱密码分开，避免 Outlook/Gmail 账号录入 OpenAI 密码时
    破坏原有收件凭证；通用 OTP 旧格式仍会继续从 ``password`` 兼容读取。

    返回 ``registered`` 或 ``pool`` 表示实际更新位置，邮箱不存在返回 None。
    """
    key = str(email or "").strip().lower()
    value = str(password or "").strip()
    if not key or not value:
        return None
    with _lock:
        con = _conn()
        reg = con.execute("SELECT email FROM registered WHERE email=?", (key,)).fetchone()
        if reg:
            con.execute("UPDATE registered SET password=? WHERE email=?", (value, key))
            con.commit()
            return "registered"
        pool = con.execute("SELECT email FROM outlook_accounts WHERE email=?", (key,)).fetchone()
        if not pool:
            return None
        con.execute("UPDATE outlook_accounts SET login_password=? WHERE email=?", (value, key))
        con.commit()
        return "pool"


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
        " AND COALESCE(r.account_status, 'active') <> 'permanently_invalid') AS active_registered_total, "
        "(SELECT COUNT(*) FROM outlook_accounts a "
        " LEFT JOIN registered r ON r.email=a.email "
        " WHERE a.group_name=g.name AND r.email IS NULL "
        " AND a.status IN ('available', 'done', 'failed') "
        " AND a.kind='icloud_relay' "
        " AND length(trim(COALESCE(a.password, ''))) > 0 "
        " AND length(trim(COALESCE(a.relay_url, ''))) > 0) AS mailbox_only_total "
        "FROM account_groups g ORDER BY g.name COLLATE NOCASE"
    ).fetchall()
    return [
        {
            "name": row["group_name"] or "",
            "total": row["total"],
            "available": row["available"] or 0,
            "registered_total": row["registered_total"] or 0,
            "active_registered_total": row["active_registered_total"] or 0,
            # 这些是已经导入 OpenAI 密码 + OTP 收件凭证、但尚未在本地
            # 注册结果表落库的外部账号。开启“补齐2FA”时，它们才属于可投送对象。
            "mailbox_only_total": row["mailbox_only_total"] or 0,
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
        "register_mode", "register_timezone", "register_language",
    }}
    with _lock:
        con = _conn()
        pool_row = con.execute(
            "SELECT group_name, kind, password FROM outlook_accounts WHERE email=?", (email,)
        ).fetchone()
        existing_row = con.execute(
            "SELECT password, totp_secret, totp_factor_id, group_name, mail_kind, account_status, "
            "register_mode, register_timezone, register_language "
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
        register_mode = str(d.get("register_mode") or "").strip().lower()
        if register_mode not in {"protocol", "camoufox", "import"}:
            register_mode = ""
        register_timezone = str(d.get("register_timezone") or "").strip()
        register_language = str(d.get("register_language") or "").strip()
        if existing_row:
            if not password and (existing_row["password"] or "").strip():
                password = existing_row["password"]
            if not totp_secret and (existing_row["totp_secret"] or "").strip():
                totp_secret = existing_row["totp_secret"]
                # factor_id 跟着 secret 走：本轮没绑就沿用旧的
                totp_factor_id = totp_factor_id or (existing_row["totp_factor_id"] or "")
            # 注册方式是账号的既定事实：本轮没带值不清空，带了就更新。
            # registrar 只在「真注册」run 里带值（仅登录/已有账号登录链都不带），
            # 所以这里不会出现「协议登录把 camoufox 刷成 protocol」的覆盖。
            if not register_mode:
                register_mode = (existing_row["register_mode"] or "")
            # 注册时区/语言同理：协议注册/仅登录本轮没有浏览器指纹，
            # 不能用空值把上一轮 Camoufox 记录的值清掉。
            if not register_timezone:
                register_timezone = (existing_row["register_timezone"] or "")
            if not register_language:
                register_language = (existing_row["register_language"] or "")
        # 通用 OTP 旧两段导入没有 OpenAI 密码列。账号后续通过正常注册/登录
        # 已经拿到密码时，把它回写到池行，之后“补齐2FA”即可判断该账号具备
        # 密码前置条件；不会覆盖用户重新导入的非空密码。
        if pool_row and pool_row["kind"] == "icloud_relay" and password and not (pool_row["password"] or "").strip():
            con.execute(
                "UPDATE outlook_accounts SET password=? WHERE email=?",
                (password, email),
            )
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, group_name, mail_kind, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, account_status, extra_json, register_mode, "
            "register_timezone, register_language, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                register_mode,
                register_timezone,
                register_language,
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


# ──────────────────────── 注册追溯 ────────────────────────


def record_register_failure(
    email: str,
    run_id: str = "",
    error: str = "",
    category: str = "",
) -> None:
    """一次注册语义的 run 失败：累计 fail_count 并记下最近一次失败详情。

    只在「注册」任务里调（仅登录/凭证获取不是注册事件，不计入）。
    与 runs 表互补：runs 是一 run 一行的事件流，这里是按账号累计的持久计数，
    且对「失败到从未成功、registered 里没行」的账号同样生效。
    """
    email = (email or "").strip().lower()
    if not email:
        return
    now = time.time()
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO register_trace "
            "(email, fail_count, last_fail_at, last_fail_error, last_fail_category, "
            " last_run_id, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "fail_count=register_trace.fail_count+1, "
            "last_fail_at=excluded.last_fail_at, "
            "last_fail_error=excluded.last_fail_error, "
            "last_fail_category=excluded.last_fail_category, "
            "last_run_id=excluded.last_run_id, "
            "updated_at=excluded.updated_at",
            (email, now, (error or "")[:500], str(category or ""),
             str(run_id or ""), now, now),
        )
        con.commit()


def record_register_success(
    email: str,
    run_id: str = "",
    mode: str = "",
    ip: str = "",
    region: str = "",
    timezone: str = "",
    language: str = "",
    register_event: bool = True,
) -> None:
    """注册语义的 run 成功时写追溯行。

    ``register_event=True`` 表示本轮真的执行了新注册（非仅登录、非服务端识别
    为已有账号后走登录链）：出口 IP/地区/注册方式/时区/语言 与 registered_at
    只在此时落值 —— 这些字段是「注册那一刻的事实」，登录性质的 run 提供的
    值不算数。register_event=False 时只更新 last_success_at/last_run_id，
    绝不覆盖已记录的注册事实（camoufox 注册的号跑协议登录不能刷成 protocol）。

    各字段均为「非空才覆盖」语义：本轮没带值不清空历史值。
    registered_at 只保留最早一次（COALESCE），重跑注册不刷新。
    """
    email = str(email or "").strip().lower()
    if not email:
        return
    now = time.time()
    ip = str(ip or "").strip()
    region = str(region or "").strip()
    mode = str(mode or "").strip().lower()
    if mode not in {"protocol", "camoufox", "import"}:
        mode = ""
    timezone = str(timezone or "").strip()
    language = str(language or "").strip()
    with _lock:
        con = _conn()
        if register_event:
            con.execute(
                "INSERT INTO register_trace "
                "(email, register_ip, register_region, register_mode, register_timezone, "
                " register_language, registered_at, last_success_at, last_run_id, "
                " created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET "
                "register_ip=CASE WHEN excluded.register_ip<>'' "
                "  THEN excluded.register_ip ELSE register_trace.register_ip END, "
                "register_region=CASE WHEN excluded.register_region<>'' "
                "  THEN excluded.register_region ELSE register_trace.register_region END, "
                "register_mode=CASE WHEN excluded.register_mode<>'' "
                "  THEN excluded.register_mode ELSE register_trace.register_mode END, "
                "register_timezone=CASE WHEN excluded.register_timezone<>'' "
                "  THEN excluded.register_timezone ELSE register_trace.register_timezone END, "
                "register_language=CASE WHEN excluded.register_language<>'' "
                "  THEN excluded.register_language ELSE register_trace.register_language END, "
                "registered_at=COALESCE(register_trace.registered_at, excluded.registered_at), "
                "last_success_at=excluded.last_success_at, "
                "last_run_id=excluded.last_run_id, "
                "updated_at=excluded.updated_at",
                (email, ip, region, mode, timezone, language,
                 now, now, str(run_id or ""), now, now),
            )
        else:
            con.execute(
                "INSERT INTO register_trace "
                "(email, last_success_at, last_run_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET "
                "last_success_at=excluded.last_success_at, "
                "last_run_id=excluded.last_run_id, "
                "updated_at=excluded.updated_at",
                (email, now, str(run_id or ""), now, now),
            )
        con.commit()


def get_register_trace(email: str) -> Optional[dict]:
    """按邮箱取注册追溯行；没有记录返回 None。"""
    con = _conn()
    row = con.execute(
        "SELECT * FROM register_trace WHERE email=?",
        ((email or "").strip().lower(),),
    ).fetchone()
    return dict(row) if row else None


# 列表/批量/详情共用的追溯 SELECT：trace 全字段 + registered 概要
# （分组/邮箱类型/账号状态/是否已有凭证行），不带任何凭证密文。
_TRACE_LIST_SELECT = """
    SELECT t.*,
           COALESCE(r.group_name, '')    AS group_name,
           COALESCE(r.mail_kind, '')     AS mail_kind,
           COALESCE(r.account_status, '') AS account_status,
           (r.email IS NOT NULL)          AS has_credentials
      FROM register_trace t
      LEFT JOIN registered r ON r.email = t.email
"""


def list_register_traces(
    limit: int = 200,
    offset: int = 0,
    email: str = "",
    only_failed: bool = False,
) -> list[dict]:
    """注册追溯列表（供外部程序查询）。

    ``email`` 是子串过滤；``only_failed`` 只列出有过失败记录的账号。
    """
    conditions: list[str] = []
    args: list = []
    em = (email or "").strip().lower()
    if em:
        conditions.append("t.email LIKE ?")
        args.append(f"%{em}%")
    if only_failed:
        conditions.append("t.fail_count > 0")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    con = _conn()
    cur = con.execute(
        f"{_TRACE_LIST_SELECT} {where} "
        f"ORDER BY COALESCE(t.registered_at, t.created_at) DESC "
        f"LIMIT ? OFFSET ?",
        [*args, limit, offset],
    )
    return [dict(r) for r in cur.fetchall()]


def count_register_traces(email: str = "", only_failed: bool = False) -> int:
    conditions: list[str] = []
    args: list = []
    em = (email or "").strip().lower()
    if em:
        conditions.append("t.email LIKE ?")
        args.append(f"%{em}%")
    if only_failed:
        conditions.append("t.fail_count > 0")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    con = _conn()
    cur = con.execute(
        f"SELECT COUNT(*) FROM register_trace t "
        f"LEFT JOIN registered r ON r.email = t.email {where}",
        args,
    )
    return cur.fetchone()[0]


def get_register_trace_full(email: str) -> Optional[dict]:
    """单账号追溯详情：trace 行 + registered 概要。

    账号在册但没有 trace 行（理论上不会出现）时，从 registered 合成一条
    兜底行，保证「账号存在」和「从没被系统见过」两种 404 语义分得开。
    """
    em = (email or "").strip().lower()
    if not em:
        return None
    con = _conn()
    row = con.execute(
        f"{_TRACE_LIST_SELECT} WHERE t.email=?", (em,),
    ).fetchone()
    if row:
        return dict(row)
    r = con.execute(
        "SELECT email, group_name, mail_kind, account_status, register_mode, "
        "register_timezone, register_language, created_at "
        "FROM registered WHERE email=?",
        (em,),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    return {
        "email": em,
        "register_ip": "",
        "register_region": "",
        "register_mode": d.get("register_mode") or "",
        "register_timezone": d.get("register_timezone") or "",
        "register_language": d.get("register_language") or "",
        "registered_at": d.get("created_at"),
        "last_success_at": None,
        "fail_count": 0,
        "last_fail_at": None,
        "last_fail_error": "",
        "last_fail_category": "",
        "last_run_id": "",
        "created_at": d.get("created_at"),
        "updated_at": d.get("created_at"),
        "group_name": d.get("group_name") or "",
        "mail_kind": d.get("mail_kind") or "",
        "account_status": d.get("account_status") or "",
        "has_credentials": 1,
    }


def get_register_traces_by_emails(emails: list[str]) -> dict[str, dict]:
    """批量按邮箱查追溯。返回 {email: 行}，查不到的 email 不进 map。"""
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not cleaned:
        return {}
    con = _conn()
    out: dict[str, dict] = {}
    CHUNK = 500
    for i in range(0, len(cleaned), CHUNK):
        part = cleaned[i:i + CHUNK]
        marks = ",".join("?" * len(part))
        cur = con.execute(
            f"{_TRACE_LIST_SELECT} WHERE t.email IN ({marks})", part,
        )
        for row in cur.fetchall():
            d = dict(row)
            out[d["email"]] = d
    return out


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
                "account_status, extra_json, register_mode, register_timezone, register_language, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    json.dumps(item["extra"], ensure_ascii=False),
                    # 已有真实注册方式的老记录不覆盖；没有的才标 import。
                    ((old["register_mode"] or "") if old else "") or "import",
                    # 注册时区/语言是注册时刻的事实，导入不覆盖。
                    ((old["register_timezone"] or "") if old else ""),
                    ((old["register_language"] or "") if old else ""),
                    time.time(),
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
    # 导入也落一条追溯：真实注册时间不可得，以导入时刻近似；方式标 import。
    # ⚠️ 不能放进上面的 _lock 块里：record_register_success 自己会再取锁，
    #    _lock 非可重入，嵌套调用会死锁。
    for item in prepared:
        try:
            record_register_success(item["email"], mode="import")
        except Exception:
            pass
    return {"imported": len(prepared), "skipped": 0}


def import_2fa_registered(text: str, group_name: str = "") -> dict:
    """导入 邮箱----密码----2FA 格式的已注册账号。

    这些账号已经在外部注册过了，直接写进 registered 表标记为 active。
    格式：每行一个，用 ---- 分隔，支持 2 段（邮箱----密码）或 3 段（邮箱----密码----2FA）。
    空行和 # 开头的注释行自动跳过。

    **全对才写**：只要有一行不合法就整批拒绝，一个都不写库。
    """
    lines = text.strip().splitlines()
    prepared = []
    errors = []
    for idx, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) < 2 or len(parts) > 3:
            errors.append({"line": idx, "error": f"需要 2~3 段（邮箱----密码 或 邮箱----密码----2FA），实际 {len(parts)} 段"})
            continue
        email = parts[0].strip().lower()
        password = parts[1].strip()
        secret = parts[2].strip() if len(parts) == 3 else ""
        if not email or "@" not in email:
            errors.append({"line": idx, "error": "邮箱格式不对"})
            continue
        if not password:
            errors.append({"line": idx, "error": "密码不能为空"})
            continue
        if secret:
            try:
                secret = normalize_totp_secret(secret)
            except ValueError as e:
                errors.append({"line": idx, "error": f"{email}: {e}"})
                continue
        prepared.append({"email": email, "password": password, "totp_secret": secret})
    if errors:
        raise ValueError(json.dumps(
            {"message": "2FA 导入校验失败", "errors": errors},
            ensure_ascii=False,
        ))
    if not prepared:
        raise ValueError("没有有效行可导入")

    with _lock:
        con = _conn()
        imported = 0
        updated = 0
        for item in prepared:
            old = con.execute(
                "SELECT * FROM registered WHERE email=?", (item["email"],)
            ).fetchone()

            def keep(new, key):
                return new if new else ((old[key] or "") if old else "")

            grp = group_name if group_name else ((old["group_name"] or "") if old else "")
            account_status = ((old["account_status"] or "active") if old else "active")
            con.execute(
                "INSERT OR REPLACE INTO registered "
                "(email, group_name, mail_kind, password, access_token, session_token, refresh_token, "
                "id_token, device_id, csrf_token, cookie_header, totp_secret, totp_factor_id, "
                "account_status, extra_json, register_mode, register_timezone, register_language, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item["email"], grp,
                    ((old["mail_kind"] or "") if old else ""),
                    item["password"],
                    keep("", "access_token"), keep("", "session_token"),
                    keep("", "refresh_token"), keep("", "id_token"),
                    keep("", "device_id"), keep("", "csrf_token"),
                    keep("", "cookie_header"), keep(item["totp_secret"], "totp_secret"),
                    keep("", "totp_factor_id"),
                    account_status,
                    json.dumps({"import_2fa": True}, ensure_ascii=False),
                    ((old["register_mode"] or "") if old else "") or "import",
                    ((old["register_timezone"] or "") if old else ""),
                    ((old["register_language"] or "") if old else ""),
                    time.time(),
                ),
            )
            if old:
                updated += 1
            else:
                imported += 1
        con.commit()
    for item in prepared:
        try:
            record_register_success(item["email"], mode="import")
        except Exception:
            pass
    return {"imported": imported, "updated": updated, "total": len(prepared)}


def save_password_early(email: str, password: str) -> None:
    """密码候选一经本地确定就落盘，不等整个注册流程跑完。

    由 AuthFlow 的 on_password 回调触发（密码候选生成/确定时）。
    此刻账号+密码可能正在提交，本地还要过发码/验证/建账户三关，
    挂在任何一关都走不到 save_registered ——
    即使网络响应丢失，下一轮也能使用同一个密码继续流程。

    只写 email + password；token 三件套留空，等流程跑通后 save_registered
    用同一个 email 主键覆盖同一行补上。extra_json 打 pending 标记，
    方便一眼认出"有密码没凭证"的半成品行（跑通后会被 save_registered 清掉）。

    ⚠️ 行已存在时只在密码列为空时写入，绝不覆盖已有密码和 token：
       并发重跑不能把历史候选替换成另一个新候选。
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
            # 账号密码一旦成功创建就是远端持久状态。并发重试时新流程可能
            # 重新生成候选密码，但不能覆盖已有历史密码；只有本地为空时才写入。
            "ON CONFLICT(email) DO UPDATE SET password="
            "CASE WHEN trim(COALESCE(registered.password, '')) <> '' "
            "THEN registered.password ELSE excluded.password END",
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
    if normalized in {"prolite", "pro_lite", "advanced", "advanced_seat", "premium", "premium_seat", "pro", "高级", "高级席位"}:
        return "prolite"
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


def update_workspace_candidate_seat_type(workspace_master_id: int, email: str, seat_type: str) -> None:
    """更新候选人的席位类型并保留现有成员关系。

    待接受邀请尚未有 member_id，不能使用
    ``update_workspace_candidate_member``；该函数专门用于把邀请记录中的
    seat_type 写回候选表，供待邀请列表展示。
    """
    with _lock:
        con = _conn()
        canonical_seat = _canonical_workspace_seat_type(seat_type)
        codex_seat = "Codex席位" if canonical_seat == "usage_based" else ""
        gpt_seat = "GPT席位" if canonical_seat == "default" else ""
        con.execute(
            "UPDATE workspace_candidates SET seat_type=?, codex_seat=?, gpt_seat=?, updated_at=? "
            "WHERE workspace_master_id=? AND email=?",
            (
                canonical_seat or str(seat_type or "").strip(),
                codex_seat,
                gpt_seat,
                time.time(),
                int(workspace_master_id),
                str(email).lower(),
            ),
        )
        con.commit()


def update_plus_check(email: str, plus_info: dict) -> None:
    """把 Plus 检查结果写入 extra_json.plus_check。"""
    email = str(email or "").strip().lower()
    if not email:
        return
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
    is_banned = str(plus_info.get("status") or "").strip().lower() == "banned"
    if is_banned:
        # 封号与永久失效是同一个账号状态；保留检测详情，同时同步候选关系。
        extra["permanently_invalid"] = True
        extra.setdefault("permanently_invalid_reason", "Plus 检测判定封号")
    with _lock:
        if is_banned:
            con.execute(
                "UPDATE registered SET account_status='permanently_invalid', extra_json=? WHERE email=?",
                (json.dumps(extra, ensure_ascii=False), email),
            )
            con.execute(
                "UPDATE workspace_candidates SET status='permanently_invalid', updated_at=? WHERE email=?",
                (time.time(), email),
            )
        else:
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
        canonical_seat = _canonical_workspace_seat_type(seat_type)
        stored_seat = canonical_seat or str(seat_type or "").strip()
        codex_seat = "Codex席位" if canonical_seat == "usage_based" else ""
        gpt_seat = "GPT席位" if canonical_seat == "default" else ""
        con.execute(
            "UPDATE workspace_candidates SET member_id=?, seat_type=?, codex_seat=?, gpt_seat=?, updated_at=? "
            "WHERE workspace_master_id=? AND email=?",
            (member, stored_seat, codex_seat, gpt_seat, time.time(), wid, key),
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


def _registered_condition_clause(filt: str) -> str:
    """单个筛选标签的 WHERE 片段。空串 = 该标签不加约束（all / 未知值）。"""
    banned_check = "(COALESCE(CASE WHEN json_valid(r.extra_json) THEN json_extract(r.extra_json, '$.plus_check.status') ELSE '' END, '')='banned')"
    if filt == "has_at":
        return (
            f"COALESCE(r.account_status, 'active') <> 'permanently_invalid' AND NOT {banned_check} "
            "AND length(COALESCE(r.access_token, '')) > 0"
        )
    if filt == "no_at":
        # 只列还能补 AT 的号：永久失效/封号的号补不回来，全选后批量重登录会白跑。
        return (
            f"COALESCE(r.account_status, 'active') <> 'permanently_invalid' AND NOT {banned_check} "
            "AND length(COALESCE(r.access_token, '')) = 0"
        )
    if filt == "has_rt":
        return "length(r.refresh_token) > 0"
    if filt == "no_rt":
        return "coalesce(length(r.refresh_token),0) = 0"
    if filt == "unchecked":
        return "(r.extra_json IS NULL OR r.extra_json NOT LIKE '%\"plus_check\"%')"
    if filt == "free":
        return "r.extra_json LIKE '%\"free\"%'"
    if filt == "plus":
        return "(r.extra_json LIKE '%\"plus_eligible\"%' OR r.extra_json LIKE '%\"plus_active\"%')"
    if filt == "plus_active":
        return (
            "json_valid(r.extra_json) AND json_extract(r.extra_json, '$.plus_check.status')='plus_active'"
        )
    if filt == "plus_eligible":
        return (
            "json_valid(r.extra_json) AND json_extract(r.extra_json, '$.plus_check.status')='plus_eligible'"
        )
    if filt == "banned":
        # 兼容旧筛选值；封号与永久失效现在属于同一类型。
        return (
            f"(COALESCE(r.account_status, 'active')='permanently_invalid' OR {banned_check})"
        )
    if filt == "permanently_invalid":
        return (
            f"(COALESCE(r.account_status, 'active')='permanently_invalid' OR {banned_check})"
        )
    if filt == "token_invalid":
        # token_invalid 从 2026-08-10 起会写库，得能筛出来，否则等于埋了：
        # 它既不在 unchecked 里（已有结论），又不在 free/plus/banned 里。
        return "r.extra_json LIKE '%\"token_invalid\"%'"
    if filt == "no_workspace":
        # 还没划分进任何母号空间的注册结果。
        return "r.email NOT IN (SELECT email FROM workspace_candidates)"
    return ""


def _registered_filter_tokens(filt) -> list[str]:
    """filter 参数规整：支持 str（逗号分隔）或 iterable；'all'/空值剔除。"""
    if filt is None:
        return []
    if isinstance(filt, str):
        raw = filt.split(",")
    else:
        raw = []
        for item in filt:
            raw.extend(str(item).split(","))
    seen: list[str] = []
    for tok in raw:
        tok = str(tok).strip().lower()
        if not tok or tok == "all" or tok in seen:
            continue
        seen.append(tok)
    return seen


def _registered_conditions(filt, group_name: str | None = None) -> tuple[str, list]:
    # ⚠️ 所有列都要带 r. 前缀：list_registered 会 LEFT JOIN register_trace，
    # 两表有同名列（email / created_at / register_mode 等），裸列名会报
    # ambiguous column。count_registered 的 FROM 也用 registered r 别名对齐。
    conditions: list[str] = []
    args: list = []
    for tok in _registered_filter_tokens(filt):
        clause = _registered_condition_clause(tok)
        if clause:
            conditions.append(clause)
    if group_name is not None and group_name != "__all__":
        conditions.append("r.group_name=?")
        args.append(_normalize_group_name(group_name))
    return (("WHERE " + " AND ".join(conditions)) if conditions else "", args)


def count_registered(filter_rt: str = "all", group_name: str | None = None) -> int:
    con = _conn()
    where, args = _registered_conditions(filter_rt, group_name)
    cur = con.execute(f"SELECT COUNT(*) FROM registered r {where}", args)
    return cur.fetchone()[0]


def list_registered(
    limit: int = 20, offset: int = 0, filter_rt: str = "all",
    group_name: str | None = None,
) -> list[dict]:
    con = _conn()
    where, args = _registered_conditions(filter_rt, group_name)
    invalid_check = "(COALESCE(r.account_status, 'active')='permanently_invalid' OR (COALESCE(CASE WHEN json_valid(r.extra_json) THEN json_extract(r.extra_json, '$.plus_check.status') ELSE '' END, '')='banned'))"
    cur = con.execute(
        f"SELECT r.email, r.group_name, "
        f"CASE WHEN {invalid_check} THEN '' ELSE r.password END AS password, "
        f"CASE WHEN {invalid_check} THEN '' ELSE r.totp_secret END AS totp_secret, "
        f"CASE WHEN {invalid_check} THEN 'permanently_invalid' ELSE r.account_status END AS account_status, "
        f"CASE WHEN {invalid_check} THEN 0 ELSE length(r.access_token) END AS at_len, "
        f"CASE WHEN {invalid_check} THEN 0 ELSE length(r.session_token) END AS st_len, "
        f"CASE WHEN {invalid_check} THEN 0 ELSE length(r.refresh_token) END AS rt_len, "
        f"r.register_mode, r.register_timezone, r.register_language, r.extra_json, r.created_at, "
        # 注册追溯：出口 IP/地区、首次注册时间、历史失败计数与最近一次失败详情。
        f"COALESCE(t.register_ip, '') AS register_ip, "
        f"COALESCE(t.register_region, '') AS register_region, t.registered_at, "
        f"COALESCE(t.fail_count, 0) AS fail_count, "
        f"t.last_fail_at, t.last_fail_error, t.last_fail_category "
        f"FROM registered r LEFT JOIN register_trace t ON t.email = r.email "
        f"{where} ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
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


def list_login_candidates(
    group_name: str | None = "",
    filter_rt: str = "all",
    *,
    include_mailbox_only: bool = False,
    require_2fa_inputs: bool = False,
) -> list[dict]:
    """为一次“仅登录”任务生成稳定快照。

    默认只返回 ``registered`` 里的账号，保持旧的“刷新已有结果”语义。
    开启“补齐2FA”时，额外把带有 OpenAI 密码和 OTP 收件凭证、尚未写入
    ``registered`` 的号池行也加入快照。通用 OTP 外部账号建议用
    ``email----OpenAI密码----中转链接`` 导入；没有密码的历史两段格式不会
    被送入登录队列。

    号池行不会在这里 claim：仅登录任务本来就不应改变邮箱池状态；成功后由
    ``save_registered`` 写入结果表，下一次快照自然会走已注册分支并去重。
    """
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
        "a.password AS mail_password, a.login_password AS pool_login_password, "
        "a.client_id, a.refresh_token, "
        "a.relay_url, a.kind AS pool_kind "
        "FROM registered r LEFT JOIN outlook_accounts a ON a.email=r.email "
        f"{where} ORDER BY r.created_at ASC",
        args,
    ).fetchall()
    candidates = []
    for row in rows:
        kind = row["pool_kind"] or row["mail_kind"] or "outlook"
        login_password = str(row["login_password"] or "").strip()
        relay_url = str(row["relay_url"] or "").strip()
        # “补齐2FA”不再创建密码。通用 OTP 账号还必须保留自己的中转链接，
        # 因为慢路径绑定 2FA 时服务端会再次发送邮箱 OTP。Outlook 等原生
        # 邮箱 provider 仍沿用其 refresh/IMAP 凭证取码，不要求 relay_url。
        if require_2fa_inputs and not login_password:
            continue
        if require_2fa_inputs and kind == "icloud_relay" and not relay_url:
            continue
        candidates.append({
            "email": row["email"],
            "password": row["mail_password"] or "",
            "client_id": row["client_id"] or "",
            "refresh_token": row["refresh_token"] or "",
            "relay_url": relay_url,
            "kind": kind,
            "login_password": login_password,
            "totp_secret": row["totp_secret"] or "",
            "group_name": row["group_name"] or "",
        })

    if not include_mailbox_only:
        return candidates

    # mailbox-only 行没有 OpenAI refresh_token；“仅已有 RT”筛选不能把它们
    # 当成有 RT 的注册结果混入。
    if filter_rt == "has_rt":
        return candidates

    # 外部导入的账号还没有 registered 行。排除 in_use（正在被普通注册任务
    # 使用）；failed 也纳入“开启补齐”的恢复快照，因为旧版本在发现“已有账号、
    # 无法创建密码”时会把这些外部账号误记为 failed，用户不应先手工重置。
    pool_conditions = [
        "a.status IN ('available', 'done', 'failed')",
        "NOT EXISTS (SELECT 1 FROM registered r WHERE r.email=a.email)",
    ]
    pool_args: list = []
    if group_name is not None and group_name != "__all__":
        pool_conditions.append("a.group_name=?")
        pool_args.append(_normalize_group_name(group_name))
    pool_rows = con.execute(
        "SELECT a.email, a.password, a.login_password AS pool_login_password, "
        "a.client_id, a.refresh_token, "
        "a.relay_url, a.kind, a.group_name, a.imported_at "
        "FROM outlook_accounts a WHERE "
        + " AND ".join(pool_conditions)
        + " ORDER BY a.imported_at ASC",
        pool_args,
    ).fetchall()

    existing_emails = {
        str(row.get("email") or "").strip().lower()
        for row in candidates
    }
    for row in pool_rows:
        email = str(row["email"] or "").strip().lower()
        if not email or email in existing_emails:
            continue
        kind = row["kind"] or "outlook"
        relay_url = str(row["relay_url"] or "").strip()
        # 外部导入账号参与“补齐2FA”必须同时带 OpenAI 密码和 OTP 中转链接。
        # 对通用 OTP provider 而言，2 段历史格式没有 OpenAI 密码，不能再
        # 走 passwordless 登录，也不能在任务中偷偷创建新密码。
        # 三段通用 OTP 导入的旧数据把 OpenAI 密码放在 password；邮箱列表
        # 手动录入则放在独立 login_password，优先取后者，兼容两种来源。
        if kind == "icloud_relay":
            login_password = str(
                row["pool_login_password"] or row["password"] or ""
            ).strip()
        else:
            login_password = str(row["pool_login_password"] or "").strip()
        if require_2fa_inputs and (not login_password or not relay_url):
            continue
        # ``password`` here is邮箱收件箱密码（若有），不是 OpenAI 密码；
        # 通用 OTP 的 3 段导入例外：该列承载 OpenAI 密码，provider 本身只
        # 使用 relay_url 取码。没有密码的历史两段数据会在上面被过滤。
        candidates.append({
            "email": email,
            "password": row["password"] or "",
            "client_id": row["client_id"] or "",
            "refresh_token": row["refresh_token"] or "",
            "relay_url": relay_url,
            "kind": kind,
            "login_password": login_password,
            "totp_secret": "",
            "group_name": row["group_name"] or "",
            "_login_source": "mailbox_only",
        })
        existing_emails.add(email)

    return candidates


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
    cur = con.execute(
        "SELECT r.*, "
        "COALESCE(t.register_ip, '') AS register_ip, "
        "COALESCE(t.register_region, '') AS register_region, "
        "t.registered_at, COALESCE(t.fail_count, 0) AS fail_count, "
        "t.last_fail_at, t.last_fail_error, t.last_fail_category, t.last_success_at "
        "FROM registered r LEFT JOIN register_trace t ON t.email = r.email "
        "WHERE r.email=?",
        (email.lower(),),
    )
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


def delete_account_everywhere(emails: list[str]) -> dict:
    """把账号从整个系统删掉：所有空间的候选划分、空间凭证、注册结果、号池。

    一个事务里删四张表，避免「删了一半」的中间态。
    """
    cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    counts = {"candidates": 0, "credentials": 0, "registered": 0, "pool": 0}
    if not cleaned:
        return counts
    marks = ",".join("?" * len(cleaned))
    with _lock:
        con = _conn()
        for table, key in (
            ("workspace_candidates", "candidates"),
            ("workspace_credentials", "credentials"),
            ("registered", "registered"),
            ("outlook_accounts", "pool"),
        ):
            rc = con.execute(f"DELETE FROM {table} WHERE email IN ({marks})", cleaned)
            counts[key] = rc.rowcount
        con.commit()
    return counts


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


def finish_run(
    run_id: str,
    status: str,
    error: str = "",
    category: str = "",
    email: str = "",
) -> None:
    with _lock:
        con = _conn()
        if email:
            # 非池化邮箱源（占位地址）注册出的真实邮箱在 run 结束时才确定；
            # 回写 runs.email 让调度层能按 run_id 找回账号。
            con.execute(
                "UPDATE runs SET status=?, finished_at=?, error=?, error_category=?, email=? "
                "WHERE run_id=?",
                (status, time.time(), (error or "")[:500], category or None,
                 email.lower(), run_id),
            )
        else:
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


# ─── 个人空间（Free 账号池）─────────────────────────────────────────────


_PERSONAL_SETTINGS_KEY = "personal_space_settings"


def get_personal_settings() -> dict:
    """个人空间（Free 账号池）的运行配置，单例 JSON。"""
    try:
        raw = json.loads(get_setting(_PERSONAL_SETTINGS_KEY, "{}") or "{}")
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def update_personal_settings(updates: dict) -> dict:
    """合并写个人空间配置（传入 None 的键跳过），返回合并后的完整配置。"""
    current = get_personal_settings()
    for key, value in (updates or {}).items():
        if value is None:
            continue
        current[key] = value
    set_setting(_PERSONAL_SETTINGS_KEY, json.dumps(current, ensure_ascii=False))
    return current


# ─── 个人空间成员 ─────────────────────────────────────────────────────


def assign_personal_candidates(emails: list[str]) -> int:
    """把 registered 里的账号划入个人空间。返回本轮实际新增数。

    已存在且未入箱的行不动；入箱过的行恢复成 active（重新启用）。
    """
    cleaned = sorted({str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()})
    if not cleaned:
        return 0
    now = time.time()
    changed = 0
    with _lock:
        con = _conn()
        existing = {
            r["email"]: r["trash_status"]
            for r in con.execute(
                f"SELECT email, trash_status FROM personal_candidates WHERE email IN ({','.join('?' * len(cleaned))})",
                cleaned,
            ).fetchall()
        }
        valid = {
            r["email"]
            for r in con.execute(
                f"SELECT email FROM registered WHERE email IN ({','.join('?' * len(cleaned))})",
                cleaned,
            ).fetchall()
        }
        for email in cleaned:
            if email not in valid:
                continue  # 只收 registered 里真实存在的账号
            if email not in existing:
                con.execute(
                    "INSERT INTO personal_candidates(email, trash_status, created_at, updated_at) "
                    "VALUES (?, 'active', ?, ?)",
                    (email, now, now),
                )
                changed += 1
            elif existing[email] in {"trashed", "scheduled"}:
                con.execute(
                    "UPDATE personal_candidates SET trash_status='active', trash_due_at=0, "
                    "trash_reason='', updated_at=? WHERE email=?",
                    (now, email),
                )
                changed += 1
        con.commit()
    return changed


def remove_personal_candidates(emails: list[str]) -> int:
    cleaned = sorted({str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()})
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        cur = con.execute(
            f"DELETE FROM personal_candidates WHERE email IN ({','.join('?' * len(cleaned))})",
            cleaned,
        )
        con.commit()
        return cur.rowcount


def list_personal_candidates(
    trash_status: str = "",
    keyword: str = "",
    group_name: str = "",
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """个人空间成员列表：JOIN registered 带出账号态/凭证态。"""
    clauses = ["1=1"]
    args: list = []
    if trash_status:
        normalized = str(trash_status).strip().lower()
        if normalized not in {"active", "scheduled", "trashed"}:
            raise ValueError("trash_status 只能是 active / scheduled / trashed")
        clauses.append("COALESCE(p.trash_status, 'active')=?")
        args.append(normalized)
    if group_name:
        clauses.append("r.group_name=?")
        args.append(str(group_name))
    kw = str(keyword or "").strip()
    if kw:
        like = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(p.email LIKE ? ESCAPE '\\' OR r.group_name LIKE ? ESCAPE '\\')")
        args.extend([like, like])
    sql = f"""SELECT p.email, r.group_name, r.mail_kind,
        COALESCE(r.account_status, 'active') AS account_status,
        COALESCE(p.trash_status, 'active') AS trash_status,
        COALESCE(p.trash_due_at, 0) AS trash_due_at,
        COALESCE(p.trash_reason, '') AS trash_reason,
        p.quota_json, p.created_at, p.updated_at,
        CASE WHEN COALESCE(r.account_status, 'active') <> 'permanently_invalid'
                  AND length(COALESCE(r.access_token,''))>0 THEN 1 ELSE 0 END AS has_access_token,
        CASE WHEN length(COALESCE(r.refresh_token,''))>0 THEN 1 ELSE 0 END AS has_refresh_token,
        CASE WHEN COALESCE(r.account_status, 'active')='permanently_invalid' THEN 'unavailable'
             WHEN length(COALESCE(r.access_token,''))>0 THEN 'personal_credential'
             ELSE 'none' END AS credential_status
        FROM personal_candidates p
        LEFT JOIN registered r ON r.email = p.email
        WHERE {' AND '.join(clauses)}
        ORDER BY p.created_at DESC"""
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args.extend([max(1, int(limit)), max(0, int(offset))])
    return [dict(r) for r in _conn().execute(sql, args).fetchall()]


def count_personal_candidates(trash_status: str = "active") -> int:
    row = _conn().execute(
        "SELECT COUNT(*) AS n FROM personal_candidates WHERE COALESCE(trash_status, 'active')=?",
        (str(trash_status or "active"),),
    ).fetchone()
    return int(row["n"] if row else 0)


def update_personal_quota(email: str, payload: dict) -> None:
    email = str(email or "").strip().lower()
    if not email:
        return
    now = time.time()
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE personal_candidates SET quota_json=?, updated_at=? WHERE email=?",
            (json.dumps(payload, ensure_ascii=False), now, email),
        )
        con.commit()


def update_personal_candidate_trash(
    email: str,
    *,
    status: str,
    reason: str = "",
    due_at: float = 0,
) -> None:
    """status: active / scheduled（排期入箱）/ trashed。"""
    email = str(email or "").strip().lower()
    normalized = str(status or "").strip().lower()
    if not email or normalized not in {"active", "scheduled", "trashed"}:
        return
    now = time.time()
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE personal_candidates SET trash_status=?, trash_reason=?, trash_due_at=?, updated_at=? WHERE email=?",
            (normalized, str(reason or ""), float(due_at or 0), now, email),
        )
        con.commit()


def get_personal_candidate(email: str) -> dict | None:
    email = str(email or "").strip().lower()
    if not email:
        return None
    row = _conn().execute(
        "SELECT * FROM personal_candidates WHERE email=?", (email,)
    ).fetchone()
    return dict(row) if row else None


def list_due_personal_trash(now: float | None = None, limit: int = 50) -> list[str]:
    """排期到点、待执行入箱的个人空间成员邮箱。"""
    rows = _conn().execute(
        "SELECT email FROM personal_candidates "
        "WHERE trash_status='scheduled' AND trash_due_at>0 AND trash_due_at<=? "
        "ORDER BY trash_due_at ASC LIMIT ?",
        (float(now or time.time()), max(1, int(limit))),
    ).fetchall()
    return [r["email"] for r in rows]


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


# ──────────────────────── 全局代理池租借统计 ────────────────────────


def record_proxy_lease_usage(proxy: str, task_type: str, task_detail: str = "") -> None:
    """持久化一次从代理池领取代理的事件。

    这里只记录“领取”而不是 HTTP 请求次数。同一个任务在 warmup 403 或账号重试时
    重新领取代理，会自然产生新的计数；同一会话里的后续请求不会重复累计。
    """
    proxy_value = str(proxy or "").strip()
    type_value = str(task_type or "").strip().lower()
    detail_value = str(task_detail or "").strip().lower()
    if not proxy_value or not type_value:
        return
    now = time.time()
    with _lock:
        con = _conn()
        con.execute(
            """INSERT INTO proxy_lease_usage(
                   proxy, task_type, task_detail, leased_count,
                   first_leased_at, last_leased_at
               ) VALUES (?, ?, ?, 1, ?, ?)
               ON CONFLICT(proxy, task_type, task_detail) DO UPDATE SET
                   leased_count=proxy_lease_usage.leased_count + 1,
                   last_leased_at=excluded.last_leased_at""",
            (proxy_value, type_value, detail_value, now, now),
        )
        con.commit()


def list_proxy_lease_usage() -> list[dict]:
    con = _conn()
    rows = con.execute(
        """SELECT proxy, task_type, task_detail, leased_count,
                  first_leased_at, last_leased_at
           FROM proxy_lease_usage
           ORDER BY last_leased_at DESC, proxy, task_type, task_detail"""
    ).fetchall()
    return [dict(row) for row in rows]


def proxy_lease_counts_since(
    proxies: list[str],
    since: float,
    task_type: str = "",
) -> dict[str, int]:
    """返回 *proxies* 中每个代理自 *since* 以来的累计租借次数。

    如果 task_type 非空，只统计该类型的记录；否则统计所有类型。
    不在 proxies 列表中的代理不会被返回。
    """
    if not proxies:
        return {}
    con = _conn()
    # 为保证 SQL 注入安全，用参数占位符
    placeholders = ",".join("?" for _ in proxies)
    params: list = list(proxies)
    type_clause = ""
    if task_type:
        type_clause = " AND task_type = ?"
        params.append(task_type)
    params.append(since)
    rows = con.execute(
        f"""SELECT proxy, SUM(leased_count) AS total
           FROM proxy_lease_usage
           WHERE proxy IN ({placeholders}){type_clause}
             AND last_leased_at >= ?
           GROUP BY proxy""",
        params,
    ).fetchall()
    result = {p: 0 for p in proxies}
    for row in rows:
        p = str(row["proxy"])
        if p in result:
            result[p] = int(row["total"])
    return result


def reset_proxy_lease_usage() -> float:
    """清空全局累计统计，返回新统计周期的开始时间。"""
    started_at = time.time()
    with _lock:
        con = _conn()
        con.execute("DELETE FROM proxy_lease_usage")
        con.execute(
            """INSERT INTO settings(key, value) VALUES ('proxy_usage_since', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(started_at),),
        )
        con.commit()
    return started_at


# ─── CPA 静态家宽代理租用 ─────────────────────────────────────────────
#
# 计数模型：cpa_proxy_leases 一行 = 一个候选人在本空间占用的一个家宽代理，
# 某代理的「计数」= 该 proxy 值的行数。分配取计数最少（并列时随机一条）；
# 凭证从 CPA 删除/视为删除后删行，即该代理计数 -1。

def lease_cpa_proxy(workspace_db_id: int, email: str, pool_text: str) -> str:
    """为候选人从 CPA 静态家宽池租一个代理，返回租到的代理串。

    已绑定过且绑定代理仍在当前池中的，直接复用原绑定（保证已推送凭证的
    proxy_url 稳定、计数不变）；绑定代理已不在池中时释放后重新分配。
    """
    key = str(email or "").strip().lower()
    pool = [
        line.strip()
        for line in str(pool_text or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not key or not pool:
        return ""
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT proxy FROM cpa_proxy_leases WHERE workspace_master_id=? AND email=?",
            (workspace_db_id, key),
        ).fetchone()
        if row:
            bound = str(row["proxy"] or "").strip()
            if bound in pool:
                return bound
            con.execute(
                "DELETE FROM cpa_proxy_leases WHERE workspace_master_id=? AND email=?",
                (workspace_db_id, key),
            )
        counts = {p: 0 for p in pool}
        for r in con.execute(
            "SELECT proxy, COUNT(*) AS n FROM cpa_proxy_leases "
            "WHERE workspace_master_id=? GROUP BY proxy",
            (workspace_db_id,),
        ).fetchall():
            p = str(r["proxy"] or "").strip()
            if p in counts:
                counts[p] = int(r["n"])
        minimum = min(counts.values())
        proxy = secrets.choice([p for p in pool if counts[p] == minimum])
        con.execute(
            "INSERT OR REPLACE INTO cpa_proxy_leases(workspace_master_id, email, proxy, created_at) "
            "VALUES (?, ?, ?, ?)",
            (workspace_db_id, key, proxy, time.time()),
        )
        con.commit()
    return proxy


def release_cpa_proxy(workspace_db_id: int, email: str) -> str:
    """释放候选人租用的 CPA 家宽代理（计数 -1），返回被释放的代理串。"""
    key = str(email or "").strip().lower()
    if not key:
        return ""
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT proxy FROM cpa_proxy_leases WHERE workspace_master_id=? AND email=?",
            (workspace_db_id, key),
        ).fetchone()
        if not row:
            return ""
        con.execute(
            "DELETE FROM cpa_proxy_leases WHERE workspace_master_id=? AND email=?",
            (workspace_db_id, key),
        )
        con.commit()
    return str(row["proxy"] or "").strip()


def cpa_proxy_lease_counts(workspace_db_id: int) -> dict:
    """本空间 CPA 家宽代理的当前租用计数：{proxy: 占用数}。"""
    con = _conn()
    return {
        str(r["proxy"]): int(r["n"])
        for r in con.execute(
            "SELECT proxy, COUNT(*) AS n FROM cpa_proxy_leases "
            "WHERE workspace_master_id=? GROUP BY proxy",
            (workspace_db_id,),
        ).fetchall()
    }


def get_cpa_proxy_lease(workspace_db_id: int, email: str) -> str:
    """候选人在本空间已绑定的 CPA 家宽代理；未绑定返回空串。

    只读查询，不分配、不校验池子——凭证既然已经带着这条代理推上了 CPA，
    系统内同一账号的额度查询/重登录也固定走它，保持出口 IP 一致。
    """
    key = str(email or "").strip().lower()
    if not key or not workspace_db_id:
        return ""
    con = _conn()
    row = con.execute(
        "SELECT proxy FROM cpa_proxy_leases WHERE workspace_master_id=? AND email=?",
        (int(workspace_db_id), key),
    ).fetchone()
    return str(row["proxy"] or "").strip() if row else ""


def get_cpa_proxy_lease_for_email(email: str) -> str:
    """按邮箱查任意空间下的 CPA 家宽绑定；未绑定返回空串。

    供公开 401 重登/公开额度检查这类没有空间上下文的入口使用；同一邮箱在
    多个空间各有绑定时取最近一条。
    """
    key = str(email or "").strip().lower()
    if not key:
        return ""
    con = _conn()
    row = con.execute(
        "SELECT proxy FROM cpa_proxy_leases WHERE email=? ORDER BY created_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    return str(row["proxy"] or "").strip() if row else ""


# ─── 代理冷却 (cooldown) ─────────────────────────────────────────────

def record_proxy_error(
    proxy: str,
    error_type: str,
    cooldown_seconds: int = 3600,
) -> None:
    """记录一次代理错误事件，并设置冷却到期时间。

    *error_type*: 错误分类，如 ``"unable_to_load_site"``。
    *cooldown_seconds*: 冷却时长（秒），默认 3600（1 小时）。
    """
    proxy_value = str(proxy or "").strip()
    error_type_value = str(error_type or "").strip().lower()
    if not proxy_value or not error_type_value:
        return
    now = time.time()
    until = now + cooldown_seconds
    with _lock:
        con = _conn()
        con.execute(
            """INSERT INTO proxy_cooldown(
                   proxy, error_type, error_count, last_error_at, cooldown_until
               ) VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(proxy, error_type) DO UPDATE SET
                   error_count=proxy_cooldown.error_count + 1,
                   last_error_at=excluded.last_error_at,
                   cooldown_until=excluded.cooldown_until""",
            (proxy_value, error_type_value, now, until),
        )
        con.commit()


def is_proxy_in_cooldown(proxy: str, error_type: str = "") -> bool:
    """判断代理是否仍在冷却期内。

    *error_type* 为空时，任何错误类型只要有一条冷却中即返回 True。
    """
    proxy_value = str(proxy or "").strip()
    if not proxy_value:
        return False
    now = time.time()
    con = _conn()
    if error_type:
        row = con.execute(
            "SELECT cooldown_until FROM proxy_cooldown "
            "WHERE proxy=? AND error_type=? AND cooldown_until>?",
            (proxy_value, str(error_type).strip().lower(), now),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT cooldown_until FROM proxy_cooldown "
            "WHERE proxy=? AND cooldown_until>?",
            (proxy_value, now),
        ).fetchone()
    return row is not None


def list_proxy_cooldown() -> list[dict]:
    """列出所有代理冷却记录（含已过期的）。"""
    con = _conn()
    rows = con.execute(
        """SELECT proxy, error_type, error_count, last_error_at, cooldown_until
           FROM proxy_cooldown
           ORDER BY cooldown_until DESC, proxy, error_type"""
    ).fetchall()
    return [dict(row) for row in rows]


# 候选人任务专用的代理错误分类。与 record_proxy_error 的一次即冷却不同，
# 这里要求连续多次失败才熔断——日志显示 3 条代理制造了 70% 的失败，而
# 偶发抖动的代理失败率只有 0.3%，一次就踢会把好代理误伤出池。
CANDIDATE_PROXY_ERROR_TYPE = "candidate_quota"
CANDIDATE_PROXY_FAILURE_STREAK = 3
CANDIDATE_PROXY_COOLDOWN_SECONDS = 30 * 60


def record_candidate_proxy_failure(
    proxy: str,
    *,
    streak_threshold: int = CANDIDATE_PROXY_FAILURE_STREAK,
    cooldown_seconds: int = CANDIDATE_PROXY_COOLDOWN_SECONDS,
) -> int:
    """记录候选人任务的一次代理网络失败，返回累计连击数。

    连击数达到 *streak_threshold* 才写入冷却时间；未达阈值时只累加计数，
    ``cooldown_until`` 保持 0，因此不影响租取。
    """
    proxy_value = str(proxy or "").strip()
    if not proxy_value:
        return 0
    threshold = max(1, int(streak_threshold or 1))
    now = time.time()
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT error_count FROM proxy_cooldown WHERE proxy=? AND error_type=?",
            (proxy_value, CANDIDATE_PROXY_ERROR_TYPE),
        ).fetchone()
        streak = int((row["error_count"] if row else 0) or 0) + 1
        until = now + max(1, int(cooldown_seconds or 1)) if streak >= threshold else 0.0
        con.execute(
            """INSERT INTO proxy_cooldown(
                   proxy, error_type, error_count, last_error_at, cooldown_until
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(proxy, error_type) DO UPDATE SET
                   error_count=excluded.error_count,
                   last_error_at=excluded.last_error_at,
                   cooldown_until=excluded.cooldown_until""",
            (proxy_value, CANDIDATE_PROXY_ERROR_TYPE, streak, now, until),
        )
        con.commit()
    return streak


def clear_candidate_proxy_failure(proxy: str) -> None:
    """候选人任务成功后清零该代理的连击计数与冷却。"""
    proxy_value = str(proxy or "").strip()
    if not proxy_value:
        return
    with _lock:
        con = _conn()
        con.execute(
            "DELETE FROM proxy_cooldown WHERE proxy=? AND error_type=?",
            (proxy_value, CANDIDATE_PROXY_ERROR_TYPE),
        )
        con.commit()


# ──────────────────── 公开重登录页的 403 缓刑 / 402 空间判死 ────────────────────

# 连续多少轮 403 才判定账号停用。单次 403 常是边缘节点瞬时拒绝，直接判死会把
# 还活着的号永久踢出巡检（deactivated 在公开页是终态）。与空间侧的
# workspace_membership.DEACTIVATION_403_STREAK 同口径。
PUBLIC_RELOGIN_403_STREAK = 2
# 同一个 workspace 下有多少个**不同账号**吃到 402 才宣布空间死亡。402 是母号
# 空间的订阅/付款异常，会打到该空间所有成员身上，所以按账号数而不是按次数算：
# 同一个账号 402 十次仍然只算 1 个，不会自己把空间判死。
PUBLIC_RELOGIN_402_DEAD_ACCOUNTS = 3

_PENALTY_403 = "account_403"
_PENALTY_402 = "workspace_402"
_PENALTY_DEAD = "workspace_dead"


def record_public_relogin_403(
    email: str,
    *,
    streak_threshold: int = PUBLIC_RELOGIN_403_STREAK,
) -> int:
    """记录公开页一次 403，返回累计连击数。email 为空时不记账，返回 0。

    返回 0 意味着调用方永远不会判死——没有 email 的账号本来也重登不了，
    把它标成停用只是丢信息。
    """
    key = str(email or "").strip().lower()
    if not key:
        return 0
    now = time.time()
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT count FROM public_relogin_penalty WHERE scope=? AND subject=? AND detail=''",
            (_PENALTY_403, key),
        ).fetchone()
        streak = int((row["count"] if row else 0) or 0) + 1
        con.execute(
            """INSERT INTO public_relogin_penalty(
                   scope, subject, detail, count, first_at, last_at, note
               ) VALUES (?, ?, '', ?, ?, ?, ?)
               ON CONFLICT(scope, subject, detail) DO UPDATE SET
                   count=excluded.count,
                   last_at=excluded.last_at,
                   note=excluded.note""",
            (_PENALTY_403, key, streak, now, now,
             f"连续 403 {streak}/{max(1, int(streak_threshold or 1))}"),
        )
        con.commit()
    return streak


def clear_public_relogin_403(email: str) -> None:
    """额度查询成功后清零该账号的 403 连击——缓刑期内恢复正常即销案。"""
    key = str(email or "").strip().lower()
    if not key:
        return
    with _lock:
        con = _conn()
        con.execute(
            "DELETE FROM public_relogin_penalty WHERE scope=? AND subject=? AND detail=''",
            (_PENALTY_403, key),
        )
        con.commit()


def record_public_relogin_402(
    workspace_id: str,
    email: str,
    *,
    dead_threshold: int = PUBLIC_RELOGIN_402_DEAD_ACCOUNTS,
) -> tuple[int, bool]:
    """记录一次 402，返回 (该空间命中 402 的不同账号数, 是否刚好判死)。

    workspace_id 为空时无从归属，返回 (0, False) 不记账。
    """
    workspace = str(workspace_id or "").strip()
    if not workspace:
        return 0, False
    # detail 存 email；没有 email 时用固定占位符，避免一堆匿名行各自占一个主键
    # 把账号数刷上去。
    key_email = str(email or "").strip().lower() or "-"
    threshold = max(1, int(dead_threshold or 1))
    now = time.time()
    with _lock:
        con = _conn()
        con.execute(
            """INSERT INTO public_relogin_penalty(
                   scope, subject, detail, count, first_at, last_at, note
               ) VALUES (?, ?, ?, 1, ?, ?, '')
               ON CONFLICT(scope, subject, detail) DO UPDATE SET
                   count=public_relogin_penalty.count + 1,
                   last_at=excluded.last_at""",
            (_PENALTY_402, workspace, key_email, now, now),
        )
        row = con.execute(
            "SELECT COUNT(*) AS n FROM public_relogin_penalty WHERE scope=? AND subject=?",
            (_PENALTY_402, workspace),
        ).fetchone()
        accounts = int((row["n"] if row else 0) or 0)
        dead = False
        if accounts >= threshold:
            # 墓碑用 INSERT OR IGNORE：第一次判死的时间才有意义，后续 402 不该
            # 把 first_at 往后推，所以命中账号数另用 UPDATE 刷新。
            note = f"{accounts} 个账号返回 HTTP 402"
            con.execute(
                """INSERT OR IGNORE INTO public_relogin_penalty(
                       scope, subject, detail, count, first_at, last_at, note
                   ) VALUES (?, ?, '', ?, ?, ?, ?)""",
                (_PENALTY_DEAD, workspace, accounts, now, now, note),
            )
            con.execute(
                """UPDATE public_relogin_penalty
                      SET count=?, last_at=?, note=?
                    WHERE scope=? AND subject=? AND detail=''""",
                (accounts, now, note, _PENALTY_DEAD, workspace),
            )
            dead = True
        con.commit()
    return accounts, dead


def is_public_workspace_dead(workspace_id: str) -> bool:
    workspace = str(workspace_id or "").strip()
    if not workspace:
        return False
    con = _conn()
    row = con.execute(
        "SELECT 1 FROM public_relogin_penalty WHERE scope=? AND subject=? AND detail=''",
        (_PENALTY_DEAD, workspace),
    ).fetchone()
    return row is not None


def list_dead_public_workspaces() -> list[dict]:
    con = _conn()
    rows = con.execute(
        """SELECT subject AS workspace_id, count AS account_count,
                  first_at AS dead_at, last_at, note
             FROM public_relogin_penalty
            WHERE scope=? AND detail=''
            ORDER BY first_at DESC""",
        (_PENALTY_DEAD,),
    ).fetchall()
    return [dict(row) for row in rows]


def clear_dead_public_workspace(workspace_id: str) -> bool:
    """手动解除判死。墓碑和该空间的 402 明细一起删——只删墓碑的话，解除后
    随便再来一个 402 就会立刻重新达阈值判死，等于没解除。"""
    workspace = str(workspace_id or "").strip()
    if not workspace:
        return False
    with _lock:
        con = _conn()
        cur = con.execute(
            "DELETE FROM public_relogin_penalty WHERE scope=? AND subject=? AND detail=''",
            (_PENALTY_DEAD, workspace),
        )
        removed = cur.rowcount > 0
        con.execute(
            "DELETE FROM public_relogin_penalty WHERE scope=? AND subject=?",
            (_PENALTY_402, workspace),
        )
        con.commit()
    return removed


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
        # 是否在 Sub2API 导出前用 refresh_token 换一组新的 Codex AT/ID。
        # 默认关闭：新流程保存的 AT/ID 已经同源，直接导出更快；历史混合凭证
        # 可临时打开一次进行修复。
        "sub2api_refresh_oauth": get_setting("export_sub2api_refresh_oauth", "0"),
    }


def save_export_config(data: dict) -> None:
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    # 布尔开关
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
        ("sub2api_refresh_oauth", "export_sub2api_refresh_oauth"),
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
        "refresh_oauth":      get_setting("export_sub2api_refresh_oauth", "0") in ("1", "true"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


def get_cpa_export_template() -> dict:
    """候选管理 CPA「导出模版配置」：勾选按模版导出时写进每个凭证 JSON。

    proxy_url 是 CPA 凭证级代理字段；file_enabled 对应文件里的 disabled
    （启用 → disabled=false）。默认启用、无代理。
    """
    return {
        "proxy_url": get_setting("cpa_export_tpl_proxy_url", ""),
        "file_enabled": get_setting("cpa_export_tpl_file_enabled", "1") in ("1", "true"),
    }


def save_cpa_export_template(data: dict) -> None:
    if "proxy_url" in data:
        set_setting("cpa_export_tpl_proxy_url", _setting_text(data["proxy_url"]))
    if "file_enabled" in data:
        set_setting(
            "cpa_export_tpl_file_enabled",
            "1" if _setting_bool(data["file_enabled"]) else "0",
        )


def get_public_relogin_config() -> dict:
    """公开 401 重登录页面的后台配置。"""
    return {
        "enabled": get_setting("public_relogin_enabled", "0"),
        "proxy_pool": get_setting("public_relogin_proxy_pool", ""),
        "use_system_proxy_pool": get_setting("public_relogin_use_system_proxy_pool", "1"),
        "concurrency": get_setting("public_relogin_concurrency", "3"),
        "quota_queue_capacity": get_setting("public_relogin_quota_queue_capacity", "512"),
        "relogin_queue_capacity": get_setting("public_relogin_relogin_queue_capacity", "128"),
        "retry_count": get_setting("public_relogin_retry_count", "2"),
        "quota_timeout": get_setting("public_relogin_quota_timeout", "30"),
        "login_timeout": get_setting("public_relogin_login_timeout", "180"),
        "rate_limit_retries": get_setting("public_relogin_rate_limit_retries", "2"),
        "forbidden_streak": get_setting(
            "public_relogin_forbidden_streak", str(PUBLIC_RELOGIN_403_STREAK)),
        "payment_dead_accounts": get_setting(
            "public_relogin_payment_dead_accounts", str(PUBLIC_RELOGIN_402_DEAD_ACCOUNTS)),
    }


def save_public_relogin_config(data: dict) -> None:
    if "public_relogin_enabled" in data:
        set_setting("public_relogin_enabled", "1" if _setting_bool(data["public_relogin_enabled"]) else "0")
    for key_in, key_out in (
        ("proxy_pool", "public_relogin_proxy_pool"),
        ("use_system_proxy_pool", "public_relogin_use_system_proxy_pool"),
        ("concurrency", "public_relogin_concurrency"),
        ("quota_queue_capacity", "public_relogin_quota_queue_capacity"),
        ("relogin_queue_capacity", "public_relogin_relogin_queue_capacity"),
        ("retry_count", "public_relogin_retry_count"),
        ("quota_timeout", "public_relogin_quota_timeout"),
        ("login_timeout", "public_relogin_login_timeout"),
        ("rate_limit_retries", "public_relogin_rate_limit_retries"),
        ("forbidden_streak", "public_relogin_forbidden_streak"),
        ("payment_dead_accounts", "public_relogin_payment_dead_accounts"),
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
