import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


class LoginCandidateTests(unittest.TestCase):
    def test_otp_only_mailbox_is_available_only_for_credential_completion(self):
        account = {
            "email": "external@example.com",
            "relay_url": "https://gapi.example.test/api/get-code?uid=abc",
            "kind": "icloud_relay",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path), patch.object(
                db, "parse_lines", return_value=[account],
            ):
                db.init_db()
                db.import_accounts("ignored", "icloud_relay", "outside")

                # 旧的仅登录/刷新模式只看 registered，不能把尚未落结果的
                # 外部 OTP 号误投送。
                self.assertEqual(db.list_login_candidates("outside"), [])

                rows = db.list_login_candidates(
                    "outside", include_mailbox_only=True,
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["email"], account["email"])
                self.assertEqual(rows[0]["relay_url"], account["relay_url"])
                self.assertEqual(rows[0]["login_password"], "")
                self.assertEqual(rows[0]["totp_secret"], "")
                self.assertEqual(rows[0]["_login_source"], "mailbox_only")

                # 处理成功后写入 registered，下一次快照只能出现一条，
                # 不会把同一邮箱作为 mailbox-only 再追加一次。
                db.save_registered({
                    "email": account["email"],
                    "password": "OpenAI-password",
                    "access_token": "at",
                    "session_token": "st",
                })
                rows = db.list_login_candidates(
                    "outside", include_mailbox_only=True,
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["login_password"], "OpenAI-password")

    def test_no_rt_filter_keeps_mailbox_only_candidate(self):
        account = {
            "email": "external-no-rt@example.com",
            "relay_url": "https://gapi.example.test/api/get-code?uid=no-rt",
            "kind": "icloud_relay",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path), patch.object(
                db, "parse_lines", return_value=[account],
            ):
                db.init_db()
                db.import_accounts("ignored", "icloud_relay")
                rows = db.list_login_candidates(
                    "", filter_rt="no_rt", include_mailbox_only=True,
                )
                self.assertEqual(
                    [row["email"] for row in rows], [account["email"]],
                )
                self.assertEqual(
                    db.list_login_candidates(
                        "", filter_rt="has_rt", include_mailbox_only=True,
                    ),
                    [],
                )

    def test_failed_mailbox_only_row_can_be_recovered_by_completion(self):
        account = {
            "email": "previously-failed@example.com",
            "relay_url": "https://gapi.example.test/api/get-code?uid=failed",
            "kind": "icloud_relay",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path), patch.object(
                db, "parse_lines", return_value=[account],
            ):
                db.init_db()
                db.import_accounts("ignored", "icloud_relay")
                db.mark_failed(account["email"], "旧版已有账号分支")
                rows = db.list_login_candidates(
                    "", include_mailbox_only=True,
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["email"], account["email"])

if __name__ == "__main__":
    unittest.main()
