import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


class RegisteredFilterTests(unittest.TestCase):
    def test_has_at_only_returns_active_accounts_with_access_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({"email": "with-at@example.com", "access_token": "token"})
                db.save_registered({"email": "without-at@example.com", "access_token": ""})
                db.save_registered({"email": "invalid@example.com", "access_token": "token"})
                db.mark_registered_permanently_invalid("invalid@example.com", "test")

                rows = db.list_registered(filter_rt="has_at")

                self.assertEqual([row["email"] for row in rows], ["with-at@example.com"])
                self.assertEqual(db.count_registered(filter_rt="has_at"), 1)

    def test_permanently_invalid_filter_returns_invalid_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({"email": "active@example.com", "access_token": "active-token"})
                db.save_registered({"email": "invalid@example.com", "access_token": "invalid-token"})
                db.mark_registered_permanently_invalid("invalid@example.com", "test")

                rows = db.list_registered(filter_rt="permanently_invalid")
                count = db.count_registered(filter_rt="permanently_invalid")

        self.assertEqual([row["email"] for row in rows], ["invalid@example.com"])
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
