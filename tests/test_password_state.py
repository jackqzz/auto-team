import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


class PasswordStateTests(unittest.TestCase):
    def test_early_password_save_does_not_overwrite_existing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                db.init_db()
                db.save_registered({
                    "email": "account@example.com",
                    "password": "Historical!123",
                })
                db.save_password_early("account@example.com", "NewCandidate!456")
                row = db.get_registered("account@example.com")
                self.assertEqual(row["password"], "Historical!123")

if __name__ == "__main__":
    unittest.main()
