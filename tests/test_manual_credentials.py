import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


class ManualCredentialsTests(unittest.TestCase):
    def test_add_password_keeps_tokens_and_totp_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({
                    "email": "passwordless@example.com",
                    "password": "",
                    "access_token": "access-token",
                    "session_token": "session-token",
                    "refresh_token": "refresh-token",
                    "id_token": "id-token",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                })

                changed = db.update_registered_manual(
                    "passwordless@example.com", password="WebPassword!123"
                )
                row = db.get_registered("passwordless@example.com")

                self.assertTrue(changed)
                self.assertEqual(row["password"], "WebPassword!123")
                self.assertEqual(row["access_token"], "access-token")
                self.assertEqual(row["session_token"], "session-token")
                self.assertEqual(row["refresh_token"], "refresh-token")
                self.assertEqual(row["id_token"], "id-token")
                self.assertEqual(row["totp_secret"], "JBSWY3DPEHPK3PXP")


if __name__ == "__main__":
    unittest.main()
