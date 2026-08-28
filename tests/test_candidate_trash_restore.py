import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


class CandidateTrashRestoreTests(unittest.TestCase):
    def test_db_restore_clears_trash_metadata_and_preserves_candidate_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.import_workspace_sessions(
                    "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
                    "----socks5://127.0.0.1:1080"
                )
                db.save_registered({"email": "member@example.com", "access_token": "token"})
                db.assign_workspace_candidates(1, ["member@example.com"])
                db.update_workspace_candidate_status(1, "member@example.com", "joined")
                db.update_workspace_candidate_trash(
                    1,
                    "member@example.com",
                    status="trashed",
                    reason="manual_trash",
                )

                restored = db.restore_workspace_candidates_from_trash(
                    1, ["MEMBER@example.com", "member@example.com"]
                )
                row = db.get_workspace_candidate(1, "member@example.com")

        self.assertEqual(restored, 1)
        self.assertEqual(row["trash_status"], "active")
        self.assertEqual(row["trash_due_at"], 0)
        self.assertEqual(row["trash_reason"], "")
        self.assertEqual(row["workspace_join_status"], "joined")

    def test_api_restores_only_trashed_candidates(self):
        request = app.WorkspaceCandidatesReq(
            workspace_id=7,
            emails=["TRASHED@example.com", "active@example.com"],
        )
        indexed = {
            "trashed@example.com": {"trash_status": "trashed"},
            "active@example.com": {"trash_status": "active"},
        }
        with (
            patch.object(app, "_workspace_candidate_index", return_value=indexed),
            patch.object(app.db, "restore_workspace_candidates_from_trash", return_value=1) as restore,
        ):
            result = app.api_restore_workspace_candidates_from_trash(request)

        restore.assert_called_once_with(7, ["trashed@example.com"])
        self.assertEqual(result["restored"], 1)
        self.assertEqual(result["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
