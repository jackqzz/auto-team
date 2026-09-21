import base64
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db

try:
    from webui import app, exporter
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    app = None


CODE_RE = re.compile(r"^[A-Z0-9]{12}$")


def _jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _oauth_pair(account_id="ws-ext-1"):
    access = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": "user-1",
                "chatgpt_plan_type": "team",
            },
            "exp": int(time.time()) + 3600,
        }
    )
    identity = _jwt(
        {
            "at_hash": exporter._oidc_at_hash(access),
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        }
    )
    return access, identity


def _setup_workspace(tmp_path):
    """建一个母号 + 一个有空间凭证的候选账号，返回 (wid, email)。"""
    email = "member@example.com"
    result = db.import_workspace_sessions(
        "owner@example.com----session-token-xyz", proxy="http://127.0.0.1:8080"
    )
    assert result["inserted"] == 1
    wid = db.list_workspace_masters(limit=1)[0]["id"]
    con = db._conn()
    con.execute("UPDATE workspace_masters SET workspace_id='ws-ext-1' WHERE id=?", (wid,))
    con.commit()
    con.close()
    db.save_registered({"email": email, "password": "pw-123", "totp_secret": "TOTPABC"})
    access, identity = _oauth_pair()
    db.save_workspace_credential(
        wid,
        {
            "email": email,
            "access_token": access,
            "id_token": identity,
            "refresh_token": "rt-1",
            "session_token": "st-1",
        },
    )
    return wid, email


class RedeemCodeDbTests(unittest.TestCase):
    def test_get_or_create_is_idempotent_and_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                first = db.get_or_create_redeem_codes(wid, [email])
                self.assertIn(email, first)
                self.assertTrue(CODE_RE.match(first[email]))
                second = db.get_or_create_redeem_codes(wid, [email])
                self.assertEqual(first[email], second[email])
                entry = db.get_redeem_code(first[email])
                self.assertEqual(entry["email"], email)
                self.assertEqual(entry["workspace_id"], "ws-ext-1")
                # 大小写不敏感
                self.assertEqual(db.get_redeem_code(first[email].lower())["code"], first[email])

    def test_list_mark_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                code = db.get_or_create_redeem_codes(wid, [email])[email]
                db.mark_redeem_code_used(code)
                db.mark_redeem_code_used(code)
                rows = db.list_redeem_codes(wid)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["redeem_count"], 2)
                self.assertEqual(rows[0]["has_credential"], 1)
                self.assertEqual(rows[0]["master_account"], "owner@example.com")
                self.assertTrue(rows[0]["last_redeemed_at"])
                self.assertEqual(db.delete_redeem_codes([code]), 1)
                self.assertIsNone(db.get_redeem_code(code))
                self.assertEqual(db.list_redeem_codes(), [])
                # 作废后重新生成应得到一个新码（同一账号允许再绑定）
                new_code = db.get_or_create_redeem_codes(wid, [email])[email]
                self.assertTrue(CODE_RE.match(new_code))

    def test_redeem_status_filter_in_candidate_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                db.save_registered({"email": "nocred@example.com", "password": "pw"})
                db.assign_workspace_candidates(wid, ["nocred@example.com"])

                rows = db.list_workspace_candidate_options(wid)
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(r["has_redeem_code"] == 0 for r in rows))
                self.assertEqual(
                    db.count_workspace_candidate_options(wid, redeem_status="has_code"), 0
                )

                code = db.get_or_create_redeem_codes(wid, [email])[email]
                rows = db.list_workspace_candidate_options(wid, redeem_status="has_code")
                self.assertEqual([r["email"] for r in rows], [email])
                self.assertEqual(rows[0]["redeem_code"], code)
                self.assertEqual(rows[0]["has_redeem_code"], 1)
                rows = db.list_workspace_candidate_options(wid, redeem_status="no_code")
                self.assertEqual([r["email"] for r in rows], ["nocred@example.com"])
                self.assertEqual(
                    db.count_workspace_candidate_options(wid, redeem_status="has_code"), 1
                )
                with self.assertRaises(ValueError):
                    db.list_workspace_candidate_options(wid, redeem_status="bogus")

    def test_allow_secret_defaults_off_and_syncs_on_regenerate(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                # 默认关闭
                code = db.get_or_create_redeem_codes(wid, [email])[email]
                self.assertEqual(db.get_redeem_code(code)["allow_secret"], 0)
                # 勾选开启
                same = db.get_or_create_redeem_codes(wid, [email], allow_secret=True)
                self.assertEqual(same[email], code)
                self.assertEqual(db.get_redeem_code(code)["allow_secret"], 1)
                # 再次导出时不勾选 → 收回权限
                db.get_or_create_redeem_codes(wid, [email], allow_secret=False)
                self.assertEqual(db.get_redeem_code(code)["allow_secret"], 0)

    def test_allow_secret_migration_defaults_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                code = db.get_or_create_redeem_codes(wid, [email], allow_secret=True)[email]

                # 模拟旧版库：重建一张没有 allow_secret 列的 redeem_codes
                # （SQLite 减列的标准做法），再跑 init_db 走迁移分支
                con = db._conn()
                con.executescript(
                    """ALTER TABLE redeem_codes RENAME TO _rc_old;
                       CREATE TABLE redeem_codes (
                           code TEXT PRIMARY KEY,
                           workspace_master_id INTEGER NOT NULL,
                           email TEXT NOT NULL COLLATE NOCASE,
                           created_at REAL NOT NULL,
                           redeem_count INTEGER NOT NULL DEFAULT 0,
                           last_redeemed_at REAL,
                           UNIQUE (workspace_master_id, email)
                       );
                       INSERT INTO redeem_codes
                         SELECT code,workspace_master_id,email,created_at,
                                redeem_count,last_redeemed_at FROM _rc_old;
                       DROP TABLE _rc_old;"""
                )
                con.commit()

                db.init_db()
                entry = db.get_redeem_code(code)
                self.assertIsNotNone(entry)
                self.assertEqual(entry["allow_secret"], 0)

    def test_codes_die_with_workspace_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                code = db.get_or_create_redeem_codes(wid, [email])[email]
                self.assertTrue(db.delete_workspace_master(wid))
                self.assertIsNone(db.get_redeem_code(code))


@unittest.skipIf(app is None, "当前测试环境未安装 fastapi")
class RedeemCodeApiTests(unittest.TestCase):
    def test_generate_skip_and_redeem_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                # 再塞一个没有空间凭证的候选人
                db.save_registered({"email": "nocred@example.com", "password": "pw"})
                db.assign_workspace_candidates(wid, ["nocred@example.com"])

                resp = app.api_workspace_candidates_redeem_codes(
                    app.RedeemCodesGenerateReq(
                        workspace_id=wid, emails=[email, "nocred@example.com"]
                    )
                )
                self.assertEqual(resp["count"], 1)
                self.assertEqual(resp["skipped"], ["nocred@example.com"])
                lines = resp["text"].splitlines()
                self.assertEqual(len(lines), 1)
                code = lines[0]
                self.assertTrue(CODE_RE.match(code))

                # 重复导出拿到同一个码
                again = app.api_workspace_candidates_redeem_codes(
                    app.RedeemCodesGenerateReq(workspace_id=wid, emails=[email])
                )
                self.assertEqual(again["text"], code)

                # 公开兑换 Sub2：只出绑定的那个账号，凭证加密
                redeemed = app.api_redeem(app.RedeemReq(code=code, format="sub2api"))
                self.assertEqual(redeemed["email"], email)
                doc = json.loads(base64.b64decode(redeemed["b64"]).decode("utf-8"))
                self.assertEqual(len(doc["accounts"]), 1)
                cred = doc["accounts"][0]["credentials"]
                self.assertEqual(cred["email"], email)
                self.assertEqual(cred["workspace_id"], "ws-ext-1")
                self.assertTrue(str(cred["password"]).startswith("enc:v1:"))
                self.assertTrue(str(cred["totp_secret"]).startswith("enc:v1:"))

                # CPA 格式同样只出绑定账号
                redeemed_cpa = app.api_redeem(app.RedeemReq(code=code, format="cpa"))
                cpa = json.loads(base64.b64decode(redeemed_cpa["b64"]).decode("utf-8"))
                self.assertEqual(cpa["email"], email)
                self.assertEqual(cpa["account_id"], "ws-ext-1")
                self.assertTrue(str(cpa["password"]).startswith("enc:v1:"))

                # 可重复兑换，计数累加
                self.assertEqual(db.get_redeem_code(code)["redeem_count"], 2)

    def test_generate_requires_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, _email = _setup_workspace(tmp)
                with self.assertRaises(Exception) as ctx:
                    app.api_workspace_candidates_redeem_codes(
                        app.RedeemCodesGenerateReq(
                            workspace_id=wid, emails=["nobody@example.com"]
                        )
                    )
                self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    def test_redeem_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                code = db.get_or_create_redeem_codes(wid, [email])[email]

                def expect_status(status, fn):
                    with self.assertRaises(Exception) as ctx:
                        fn()
                    self.assertEqual(getattr(ctx.exception, "status_code", None), status)

                expect_status(404, lambda: app.api_redeem(app.RedeemReq(code="DOESNOTEXIST", format="sub2api")))
                expect_status(400, lambda: app.api_redeem(app.RedeemReq(code=code, format="email_pw")))

                # 作废后不可再兑换
                db.delete_redeem_codes([code])
                expect_status(404, lambda: app.api_redeem(app.RedeemReq(code=code, format="sub2api")))

                # 凭证被删 → 410
                code2 = db.get_or_create_redeem_codes(wid, [email])[email]
                con = db._conn()
                con.execute(
                    "DELETE FROM workspace_credentials WHERE workspace_master_id=? AND email=?",
                    (wid, email),
                )
                con.commit()
                con.close()
                expect_status(410, lambda: app.api_redeem(app.RedeemReq(code=code2, format="sub2api")))

    def test_batch_redeem_merges_and_reports_failures(self):
        from webui import credential_crypto

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)

                # 第二个空间 + 第二个有凭证的账号，验证跨空间逐行加密
                db.import_workspace_sessions(
                    "owner2@example.com----session-token-two", proxy="http://127.0.0.1:8080"
                )
                masters = db.list_workspace_masters(limit=10)
                wid2 = next(m["id"] for m in masters if m["account"] == "owner2@example.com")
                con = db._conn()
                con.execute(
                    "UPDATE workspace_masters SET workspace_id='ws-ext-2' WHERE id=?", (wid2,)
                )
                con.commit()
                email2 = "member2@example.com"
                db.save_registered({"email": email2, "password": "pw-456"})
                access2, identity2 = _oauth_pair("ws-ext-2")
                db.save_workspace_credential(
                    wid2,
                    {"email": email2, "access_token": access2, "id_token": identity2,
                     "refresh_token": "rt-2"},
                )

                resp = app.api_workspace_candidates_redeem_codes(
                    app.RedeemCodesGenerateReq(workspace_id=wid, emails=[email])
                )
                code1 = resp["text"].strip()
                resp2 = app.api_workspace_candidates_redeem_codes(
                    app.RedeemCodesGenerateReq(workspace_id=wid2, emails=[email2])
                )
                code2 = resp2["text"].strip()

                # 批量：两个有效码 + 一个无效码 → 合并导出 + failed 列表
                batch = app.api_redeem(
                    app.RedeemReq(codes=[code1, "BADCODE12345", code2], format="sub2api")
                )
                self.assertEqual(batch["count"], 2)
                self.assertEqual(sorted(batch["emails"]), sorted([email, email2]))
                self.assertEqual(batch["failed"], [{"code": "BADCODE12345", "error": "兑换码不存在或已作废"}])
                doc = json.loads(base64.b64decode(batch["b64"]).decode("utf-8"))
                self.assertEqual(len(doc["accounts"]), 2)
                creds = {a["credentials"]["email"]: a["credentials"] for a in doc["accounts"]}
                # 每行用自己的 Workspace ID 加密
                self.assertEqual(
                    credential_crypto.decrypt_credential(creds[email]["password"], "ws-ext-1"),
                    "pw-123",
                )
                self.assertEqual(
                    credential_crypto.decrypt_credential(creds[email2]["password"], "ws-ext-2"),
                    "pw-456",
                )
                self.assertEqual(db.get_redeem_code(code1)["redeem_count"], 1)
                self.assertEqual(db.get_redeem_code(code2)["redeem_count"], 1)

                # CPA 批量 → ZIP 两个条目
                batch_cpa = app.api_redeem(
                    app.RedeemReq(codes=[code1, code2], format="cpa")
                )
                self.assertTrue(batch_cpa["filename"].endswith(".zip"))
                import io, zipfile

                with zipfile.ZipFile(io.BytesIO(base64.b64decode(batch_cpa["b64"]))) as z:
                    names = z.namelist()
                    self.assertEqual(len(names), 2)
                    payloads = [json.loads(z.read(n).decode("utf-8")) for n in names]
                got = {p["email"] for p in payloads}
                self.assertEqual(got, {email, email2})
                # 两个码全部失效 → 404
                db.delete_redeem_codes([code1, code2])
                with self.assertRaises(Exception) as ctx:
                    app.api_redeem(app.RedeemReq(codes=[code1, code2], format="sub2api"))
                self.assertEqual(getattr(ctx.exception, "status_code", None), 404)

    def test_secret_redeem_requires_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                # 默认未勾选 → 明文兑换被拒，提示用户该码不支持
                resp = app.api_workspace_candidates_redeem_codes(
                    app.RedeemCodesGenerateReq(workspace_id=wid, emails=[email])
                )
                code = resp["text"].strip()
                with self.assertRaises(Exception) as ctx:
                    app.api_redeem(app.RedeemReq(code=code, format="email_pw_2fa"))
                self.assertEqual(getattr(ctx.exception, "status_code", None), 403)
                self.assertIn("账号密码", str(getattr(ctx.exception, "detail", "")))
                # 被拒不计入兑换次数
                self.assertEqual(db.get_redeem_code(code)["redeem_count"], 0)
                # Sub2/CPA 不受影响，仍然可兑换
                ok = app.api_redeem(app.RedeemReq(code=code, format="sub2api"))
                self.assertEqual(ok["email"], email)

    def test_secret_redeem_enabled_returns_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                resp = app.api_workspace_candidates_redeem_codes(
                    app.RedeemCodesGenerateReq(
                        workspace_id=wid, emails=[email], allow_secret=True
                    )
                )
                code = resp["text"].strip()
                redeemed = app.api_redeem(
                    app.RedeemReq(code=code, format="email_pw_2fa")
                )
                self.assertEqual(redeemed["emails"], [email])
                self.assertEqual(
                    redeemed["text"].strip(), "member@example.com----pw-123----TOTPABC"
                )
                self.assertTrue(redeemed["filename"].endswith(".txt"))
                self.assertNotIn("b64", redeemed)
                # 重复兑换仍然可用
                again = app.api_redeem(app.RedeemReq(code=code, format="email_pw_2fa"))
                self.assertEqual(again["text"], redeemed["text"])
                self.assertEqual(db.get_redeem_code(code)["redeem_count"], 2)

    def test_secret_batch_reports_unsupported_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                wid, email = _setup_workspace(tmp)
                db.save_registered({"email": "m2@example.com", "password": "pw-9"})
                access2, identity2 = _oauth_pair()
                db.save_workspace_credential(
                    wid,
                    {"email": "m2@example.com", "access_token": access2,
                     "id_token": identity2, "refresh_token": "rt-9"},
                )
                on = app.api_workspace_candidates_redeem_codes(
                    app.RedeemCodesGenerateReq(
                        workspace_id=wid, emails=[email], allow_secret=True
                    )
                )["text"].strip()
                off = app.api_workspace_candidates_redeem_codes(
                    app.RedeemCodesGenerateReq(
                        workspace_id=wid, emails=["m2@example.com"], allow_secret=False
                    )
                )["text"].strip()
                batch = app.api_redeem(
                    app.RedeemReq(codes=[on, off], format="email_pw_2fa")
                )
                self.assertEqual(batch["emails"], [email])
                self.assertEqual(batch["text"].strip(),
                                 "member@example.com----pw-123----TOTPABC")
                self.assertEqual(len(batch["failed"]), 1)
                self.assertEqual(batch["failed"][0]["code"], off)
                self.assertIn("账号密码", batch["failed"][0]["error"])
                # 失败的码不计兑换次数
                self.assertEqual(db.get_redeem_code(off)["redeem_count"], 0)
                self.assertEqual(db.get_redeem_code(on)["redeem_count"], 1)


if __name__ == "__main__":
    unittest.main()
