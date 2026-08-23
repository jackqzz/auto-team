import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


class Sub2ApiImportTests(unittest.TestCase):
    def test_imports_registered_credentials_and_mailbox(self):
        payload = {
            "accounts": [{
                "name": "user@example.com----pickup----GptPass!",
                "platform": "openai",
                "type": "oauth",
                "credentials": {
                    "email": "user@example.com",
                    "access_token": "at",
                    "refresh_token": "rt",
                    "id_token": "id",
                },
                "notes": json.dumps({
                    "mailbox": {
                        "bind_email": "user@example.com",
                        "password": "mail-pass",
                        "client_id": "client",
                        "refresh_token": "mail-rt",
                    },
                    "gpt": {"password": "GptPass!"},
                    "two_factor": {"secret": "JBSWY3DPEHPK3PXP"},
                }),
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                result = db.import_sub2api_registered(payload, "imported")
                self.assertEqual(result["imported"], 1)
                row = db.get_registered("user@example.com")
                self.assertEqual(row["password"], "GptPass!")
                self.assertEqual(row["totp_secret"], "JBSWY3DPEHPK3PXP")
                self.assertEqual(row["refresh_token"], "rt")
                self.assertEqual(row["group_name"], "imported")
                con = sqlite3.connect(path)
                mail = con.execute(
                    "SELECT password, client_id, refresh_token FROM outlook_accounts WHERE email=?",
                    ("user@example.com",),
                ).fetchone()
                self.assertEqual(mail, ("mail-pass", "client", "mail-rt"))


if __name__ == "__main__":
    unittest.main()
