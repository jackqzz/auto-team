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

    def test_no_at_returns_active_accounts_without_access_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({"email": "with-at@example.com", "access_token": "token"})
                db.save_registered({"email": "without-at@example.com", "access_token": ""})
                db.save_registered({"email": "null-at@example.com"})
                db.save_registered({"email": "invalid@example.com", "access_token": ""})
                db.mark_registered_permanently_invalid("invalid@example.com", "test")
                db.save_registered({"email": "banned@example.com", "access_token": ""})
                db.update_plus_check("banned@example.com", {"status": "banned", "label": "封号"})

                rows = db.list_registered(filter_rt="no_at")

                # 永久失效/封号的号补不回 AT，不该混进这个筛选
                self.assertCountEqual(
                    [row["email"] for row in rows],
                    ["without-at@example.com", "null-at@example.com"],
                )
                self.assertEqual(db.count_registered(filter_rt="no_at"), 2)

    def test_has_at_and_no_at_partition_all_healthy_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({"email": "a@example.com", "access_token": "token"})
                db.save_registered({"email": "b@example.com", "access_token": ""})
                db.save_registered({"email": "c@example.com", "access_token": "token"})
                db.save_registered({"email": "dead@example.com", "access_token": "token"})
                db.mark_registered_permanently_invalid("dead@example.com", "test")

                has_at = db.count_registered(filter_rt="has_at")
                no_at = db.count_registered(filter_rt="no_at")
                invalid = db.count_registered(filter_rt="permanently_invalid")

                self.assertEqual(has_at, 2)
                self.assertEqual(no_at, 1)
                # 两个筛选互斥且合起来覆盖所有非失效账号
                self.assertEqual(has_at + no_at + invalid, db.count_registered(filter_rt="all"))

    def test_no_at_respects_group_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({"email": "g1@example.com", "access_token": ""})
                db.save_registered({"email": "g2@example.com", "access_token": ""})
                db.set_accounts_group(["g1@example.com"], "g1")
                db.set_accounts_group(["g2@example.com"], "g2")

                rows = db.list_registered(filter_rt="no_at", group_name="g1")

                self.assertEqual([row["email"] for row in rows], ["g1@example.com"])
                self.assertEqual(db.count_registered(filter_rt="no_at", group_name="g1"), 1)

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

    def test_permanently_invalid_filter_includes_plus_check_banned_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({"email": "banned@example.com", "access_token": "banned-token"})
                db.update_plus_check("banned@example.com", {"status": "banned", "label": "封号"})

                rows = db.list_registered(filter_rt="permanently_invalid")

                self.assertEqual([row["email"] for row in rows], ["banned@example.com"])
                self.assertEqual(rows[0]["account_status"], "permanently_invalid")
                self.assertEqual(rows[0]["at_len"], 0)
                self.assertEqual(db.count_registered(filter_rt="permanently_invalid"), 1)

    def test_init_db_migrates_legacy_banned_plus_check_to_permanently_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({"email": "legacy-banned@example.com", "access_token": "token"})
                con = db._conn()
                con.execute(
                    "UPDATE registered SET extra_json=? WHERE email=?",
                    ('{"plus_check":{"status":"banned","label":"封号"}}', "legacy-banned@example.com"),
                )
                con.commit()
                con.close()

                db.init_db()

                row = db.get_registered("legacy-banned@example.com")
                self.assertEqual(row["account_status"], "permanently_invalid")

    def test_plus_active_and_plus_eligible_filters_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.save_registered({"email": "active-plus@example.com", "access_token": "token"})
                db.save_registered({"email": "eligible-plus@example.com", "access_token": "token"})
                db.save_registered({"email": "both-plus@example.com", "access_token": "token"})
                db.save_registered({"email": "free-with-text@example.com", "access_token": "token"})
                db.update_plus_check("active-plus@example.com", {"status": "plus_active"})
                db.update_plus_check("eligible-plus@example.com", {"status": "plus_eligible"})
                db.update_plus_check("both-plus@example.com", {"status": "plus_active"})
                db.update_plus_check(
                    "free-with-text@example.com",
                    {"status": "free", "note": "previous plus_active marker"},
                )

                active = db.list_registered(filter_rt="plus_active")
                eligible = db.list_registered(filter_rt="plus_eligible")

        self.assertCountEqual(
            [row["email"] for row in active],
            ["both-plus@example.com", "active-plus@example.com"],
        )
        self.assertEqual([row["email"] for row in eligible], ["eligible-plus@example.com"])


if __name__ == "__main__":
    unittest.main()
