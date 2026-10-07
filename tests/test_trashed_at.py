"""垃圾箱回收时间：workspace_candidates.trashed_at。

入箱时记录回收时刻；排期（scheduled）不算回收；还原/白名单恢复时清零。
列表接口透出该字段供前端展示。
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


def _seed(tmp):
    with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
        db.init_db()
        db.import_workspace_sessions(
            "boss@example.com----session-token-abcdefghijklmnop----socks5://127.0.0.1:1080"
        )
        db.save_registered({"email": "m@example.com", "access_token": "tok"})
        db.assign_workspace_candidates(1, ["m@example.com"])
        return db


class TrashedAtTests(unittest.TestCase):
    def test_mark_trash_sets_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed(tmp)
                before = time.time()
                db.update_workspace_candidate_trash(1, "m@example.com", status="trashed", reason="manual_trash")
                rows = db.list_workspace_candidate_options(1)
                row = [r for r in rows if r["email"] == "m@example.com"][0]
                self.assertGreaterEqual(row["trashed_at"], before)
                self.assertLessEqual(row["trashed_at"], time.time() + 1)

    def test_scheduled_does_not_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed(tmp)
                db.update_workspace_candidate_trash(
                    1, "m@example.com", status="scheduled", due_at=time.time() + 600,
                    reason="quota_zero",
                )
                row = [r for r in db.list_workspace_candidate_options(1) if r["email"] == "m@example.com"][0]
                self.assertEqual(row["trashed_at"], 0)
                # 排期转正时再写入箱时刻
                db.update_workspace_candidate_trash(1, "m@example.com", status="trashed", reason="quota_zero")
                row = [r for r in db.list_workspace_candidate_options(1) if r["email"] == "m@example.com"][0]
                self.assertGreater(row["trashed_at"], 0)

    def test_restore_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed(tmp)
                db.update_workspace_candidate_trash(1, "m@example.com", status="trashed", reason="manual_trash")
                db.restore_workspace_candidates_from_trash(1, ["m@example.com"])
                row = [r for r in db.list_workspace_candidate_options(1) if r["email"] == "m@example.com"][0]
                self.assertEqual(row["trashed_at"], 0)
                self.assertEqual(row["trash_status"], "active")

    def test_mark_active_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed(tmp)
                db.update_workspace_candidate_trash(1, "m@example.com", status="trashed", reason="quota_zero")
                db.update_workspace_candidate_trash(1, "m@example.com", status="active")
                row = [r for r in db.list_workspace_candidate_options(1) if r["email"] == "m@example.com"][0]
                self.assertEqual(row["trashed_at"], 0)


if __name__ == "__main__":
    unittest.main()
