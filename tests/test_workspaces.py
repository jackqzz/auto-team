import tempfile
import unittest
from pathlib import Path
import sqlite3
from unittest.mock import patch

from webui import db


SESSION_A = "session-token-a-abcdefghijklmnopqrstuvwxyz"
SESSION_B = "session-token-b-abcdefghijklmnopqrstuvwxyz"
PROXY_A = "socks5://user:password@127.0.0.1:1080"
PROXY_B = "http://127.0.0.2:8080"


class WorkspaceMasterTests(unittest.TestCase):
    def test_import_list_update_copy_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                result = db.import_workspace_sessions(
                    f"owner@example.com----{SESSION_A}\n{SESSION_B}", proxy=PROXY_A
                )
                self.assertEqual(result["inserted"], 2)
                self.assertEqual(db.count_workspace_masters(), 2)

                items = db.list_workspace_masters(limit=20)
                self.assertEqual(len(items), 2)
                self.assertNotIn("session_token", items[0])
                self.assertNotIn("proxy_url", items[0])
                self.assertIn("session_preview", items[0])
                self.assertIn("***", items[0]["proxy_preview"])

                owner = next(item for item in items if item["account"] == "owner@example.com")
                detail = db.get_workspace_master(owner["id"])
                self.assertEqual(detail["session_token"], SESSION_A)
                self.assertEqual(detail["proxy_url"], PROXY_A)

                updated = db.import_workspace_sessions(
                    '{"email":"owner@example.com","session_token":"'
                    + SESSION_B + '-new","proxy":"' + PROXY_B + '"}'
                )
                self.assertEqual(updated["updated"], 1)
                self.assertEqual(db.count_workspace_masters(), 2)
                self.assertEqual(
                    db.get_workspace_master(owner["id"])["session_token"], SESSION_B + "-new"
                )
                self.assertEqual(db.get_workspace_master(owner["id"])["proxy_url"], PROXY_B)

                self.assertTrue(db.update_workspace_proxy(owner["id"], PROXY_A))
                self.assertEqual(db.get_workspace_master(owner["id"])["proxy_url"], PROXY_A)

                ids = [item["id"] for item in db.list_workspace_masters(limit=20)]
                self.assertEqual(db.delete_workspace_masters(ids), 2)
                self.assertEqual(db.count_workspace_masters(), 0)

    def test_invalid_batch_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                with self.assertRaisesRegex(ValueError, "session 过短"):
                    db.import_workspace_sessions(
                        f"owner@example.com----short\n{SESSION_A}", proxy=PROXY_A
                    )
                self.assertEqual(db.count_workspace_masters(), 0)

    def test_proxy_is_required_and_per_line_proxy_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                with self.assertRaisesRegex(ValueError, "必须设置专属代理"):
                    db.import_workspace_sessions(f"owner@example.com----{SESSION_A}")
                result = db.import_workspace_sessions(
                    f"owner@example.com----{SESSION_A}----{PROXY_B}"
                )
                self.assertEqual(result["inserted"], 1)
                self.assertEqual(db.get_workspace_master(1)["proxy_url"], PROXY_B)

    def test_registered_account_can_be_candidate_of_multiple_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.import_workspace_sessions(
                    f"owner-a@example.com----{SESSION_A}----{PROXY_A}\n"
                    f"owner-b@example.com----{SESSION_B}----{PROXY_B}"
                )
                db.save_registered({"email": "member@example.com", "access_token": "token"})
                self.assertEqual(db.assign_workspace_candidates(1, ["member@example.com"]), 1)
                self.assertEqual(db.assign_workspace_candidates(2, ["member@example.com"]), 1)
                self.assertEqual(len(db.list_workspace_candidates()), 2)
                self.assertEqual(db.assign_workspace_candidates(1, ["member@example.com"]), 0)

    def test_permanently_invalid_candidate_keeps_joined_status_when_member_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.import_workspace_sessions(
                    f"owner@example.com----{SESSION_A}----{PROXY_A}"
                )
                con = sqlite3.connect(str(path))
                con.execute(
                    "INSERT INTO registered(email, account_status, access_token, created_at) VALUES (?, ?, ?, ?)",
                    ("member@example.com", "permanently_invalid", "token", 1.0),
                )
                con.execute(
                    "INSERT INTO workspace_candidates("
                    "workspace_master_id, email, status, created_at, updated_at, member_id, workspace_join_status, seat_type"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (1, "member@example.com", "permanently_invalid", 1.0, 1.0, "member-123", "not_invited", ""),
                )
                con.commit()
                con.close()

                db.init_db()
                rows = db.list_workspace_candidates(1)
                self.assertEqual(rows[0]["workspace_join_status"], "joined")

                options = db.list_workspace_candidate_options(1, join_status="joined")
                self.assertEqual(len(options), 1)
                self.assertEqual(options[0]["workspace_join_status"], "joined")
                self.assertEqual(options[0]["credential_status"], "unavailable")

    def test_candidate_seat_update_refreshes_display_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.import_workspace_sessions(
                    f"owner@example.com----{SESSION_A}----{PROXY_A}"
                )
                db.save_registered({"email": "member@example.com", "access_token": "token"})
                db.assign_workspace_candidates(1, ["member@example.com"])
                db.update_workspace_candidate_member(1, "member@example.com", "member-1", "default")
                row = db.list_workspace_candidate_options(1)[0]
                self.assertEqual(row["seat_label"], "GPT席位")
                self.assertEqual(row["codex_seat"], "")
                db.update_workspace_candidate_member(1, "member@example.com", "member-1", "usage_based")
                row = db.list_workspace_candidate_options(1)[0]
                self.assertEqual(row["seat_label"], "Codex席位")
                self.assertEqual(row["gpt_seat"], "")


if __name__ == "__main__":
    unittest.main()
