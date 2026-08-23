import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


class AccountImportGroupTests(unittest.TestCase):
    def test_import_assigns_and_updates_group_without_resetting_status(self):
        account = {
            "email": "user@example.com",
            "password": "mail-pass",
            "client_id": "client",
            "refresh_token": "refresh",
            "kind": "outlook",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path), patch.object(
                db, "parse_lines", return_value=[account],
            ):
                db.init_db()

                result = db.import_accounts("ignored", "outlook", "group-one")
                self.assertEqual(result["inserted"], 1)
                self.assertEqual(db.get_account(account["email"])["group_name"], "group-one")
                self.assertEqual(db.list_groups()[0]["name"], "group-one")

                con = db._conn()
                con.execute(
                    "UPDATE outlook_accounts SET status='done' WHERE email=?",
                    (account["email"],),
                )
                con.commit()
                con.close()

                result = db.import_accounts("ignored", "outlook", "group-two")
                moved = db.get_account(account["email"])
                self.assertEqual(result["updated"], 1)
                self.assertEqual(moved["group_name"], "group-two")
                self.assertEqual(moved["status"], "done")

                # 老调用方不传 group_name 时，不能把已有账号移回未分组。
                result = db.import_accounts("ignored", "outlook")
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(db.get_account(account["email"])["group_name"], "group-two")


if __name__ == "__main__":
    unittest.main()
