"""垃圾箱白名单：白名单账号不参与自动回收；手动恢复自动入白名单；可手动启停。

锁定四件事：
- DB 层：restore 置白名单；开启白名单顺带撤掉挂起的 scheduled 排期；
  已入箱的行保持 trashed；关闭只翻标志位。
- 自动化链路全部跳过白名单：额度排期（_schedule_candidate_trash）、到期执行
  （_process_scheduled_trash_due / list_workspace_candidate_trash_due）、
  失效回收（list_invalid_workspace_candidates_pending_trash /
  trash_workspace_candidates_by_email）。
- 手动入箱/踢出不受白名单约束（显式操作优先）。
- 新列表字段 trash_whitelist 透出到候选人行数据。
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db, workspace_membership


def _workspace_with_candidate(email="member@example.com", *, account_status=None):
    db.init_db()
    db.import_workspace_sessions(
        "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
        "----socks5://127.0.0.1:1080"
    )
    db.save_registered({"email": email, "access_token": "token"})
    db.assign_workspace_candidates(1, [email])
    if account_status:
        db.mark_registered_permanently_invalid(email, reason="test")


class WhitelistDbTests(unittest.TestCase):
    def test_restore_marks_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _workspace_with_candidate()
                db.update_workspace_candidate_trash(1, "member@example.com", status="trashed", reason="quota_zero")
                restored = db.restore_workspace_candidates_from_trash(1, ["member@example.com"])
                row = db.get_workspace_candidate(1, "member@example.com")
        self.assertEqual(restored, 1)
        self.assertEqual(row["trash_status"], "active")
        self.assertEqual(row["trash_whitelist"], 1)

    def test_enable_whitelist_clears_pending_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _workspace_with_candidate()
                db.update_workspace_candidate_trash(
                    1, "member@example.com",
                    status="scheduled", due_at=time.time() + 600, reason="quota_zero",
                )
                changed = db.set_workspace_candidates_trash_whitelist(1, ["member@example.com"], enabled=True)
                row = db.get_workspace_candidate(1, "member@example.com")
        self.assertEqual(changed, 1)
        self.assertEqual(row["trash_whitelist"], 1)
        self.assertEqual(row["trash_status"], "active")
        self.assertEqual(float(row["trash_due_at"] or 0), 0.0)

    def test_enable_whitelist_keeps_trashed_rows_trashed(self):
        """白名单防的是未来的自动回收，不替代恢复操作。"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _workspace_with_candidate()
                db.update_workspace_candidate_trash(1, "member@example.com", status="trashed", reason="kicked")
                db.set_workspace_candidates_trash_whitelist(1, ["member@example.com"], enabled=True)
                row = db.get_workspace_candidate(1, "member@example.com")
        self.assertEqual(row["trash_whitelist"], 1)
        self.assertEqual(row["trash_status"], "trashed")
        self.assertEqual(row["trash_reason"], "kicked")

    def test_disable_whitelist_only_flips_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _workspace_with_candidate()
                db.set_workspace_candidates_trash_whitelist(1, ["member@example.com"], enabled=True)
                db.set_workspace_candidates_trash_whitelist(1, ["member@example.com"], enabled=False)
                row = db.get_workspace_candidate(1, "member@example.com")
        self.assertEqual(row["trash_whitelist"], 0)
        self.assertEqual(row["trash_status"], "active")

    def test_trash_due_listing_excludes_whitelisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _workspace_with_candidate()
                db.update_workspace_candidate_trash(
                    1, "member@example.com",
                    status="scheduled", due_at=time.time() - 1, reason="quota_zero",
                )
                self.assertTrue(db.list_workspace_candidate_trash_due())
                db.set_workspace_candidates_trash_whitelist(1, ["member@example.com"], enabled=True)
                self.assertFalse(db.list_workspace_candidate_trash_due())

    def test_invalid_pending_listing_excludes_whitelisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _workspace_with_candidate(account_status="permanently_invalid")
                self.assertTrue(db.list_invalid_workspace_candidates_pending_trash())
                db.set_workspace_candidates_trash_whitelist(1, ["member@example.com"], enabled=True)
                self.assertFalse(db.list_invalid_workspace_candidates_pending_trash())

    def test_options_payload_exposes_whitelist_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _workspace_with_candidate()
                db.set_workspace_candidates_trash_whitelist(1, ["member@example.com"], enabled=True)
                rows = db.list_workspace_candidate_options(1)
        row = next(r for r in rows if r["email"] == "member@example.com")
        self.assertEqual(row["trash_whitelist"], 1)


class WhitelistAutomationTests(unittest.TestCase):
    """每条自动入箱链路都要被白名单拦下。"""

    def test_schedule_skips_whitelisted(self):
        with (
            patch.object(app.db, "get_workspace_candidate", return_value={
                "trash_status": "active", "trash_whitelist": 1,
            }),
            patch.object(app.db, "update_workspace_candidate_trash") as update,
        ):
            self.assertFalse(app._schedule_candidate_trash(1, "m@example.com"))
        update.assert_not_called()

    def test_schedule_clears_stale_timer_when_whitelisted(self):
        """排期后才加的白名单：下次再排期时顺手把旧计时撤掉。"""
        with (
            patch.object(app.db, "get_workspace_candidate", return_value={
                "trash_status": "scheduled", "trash_whitelist": 1, "trash_due_at": time.time() + 600,
            }),
            patch.object(app, "_clear_candidate_trash_timer", return_value=True) as cleared,
            patch.object(app.db, "update_workspace_candidate_trash") as update,
        ):
            self.assertFalse(app._schedule_candidate_trash(1, "m@example.com"))
        cleared.assert_called_once_with(1, "m@example.com")
        update.assert_not_called()

    def test_scheduled_due_skips_whitelisted_row(self):
        """到期扫描与真正执行之间被加了白名单：撤排期放行，不碰远端。"""
        row = {"workspace_master_id": 1, "email": "m@example.com", "trash_whitelist": 1}
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_candidate_trash_enabled", return_value=True),
            patch.object(app, "_clear_candidate_trash_timer", return_value=True) as cleared,
            patch.object(app.workspace_membership, "fetch_candidate_quota") as quota,
            patch.object(app, "_apply_candidate_trash") as applied,
        ):
            result = app._process_scheduled_trash_due(row, {"trash_enabled": True}, object())
        self.assertFalse(result)
        cleared.assert_called_once_with(1, "m@example.com")
        quota.assert_not_called()
        applied.assert_not_called()

    def test_trash_by_email_skips_whitelisted_row(self):
        """停用收尾的批量入箱遇到白名单行直接跳过。"""
        with (
            patch.object(
                workspace_membership.db, "list_workspace_candidates_by_email",
                return_value=[{"workspace_master_id": 1, "trash_whitelist": 1}],
            ),
            patch.object(workspace_membership, "trash_workspace_candidate") as trash,
        ):
            result = workspace_membership.trash_workspace_candidates_by_email(
                "m@example.com", reason="quota_403",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["trashed"], 0)
        self.assertTrue(result["results"][0]["whitelisted"])
        trash.assert_not_called()

    def test_manual_trash_not_blocked_by_whitelist(self):
        """手动「移入垃圾箱」是显式操作：白名单只拦自动化，不拦手动。"""
        with (
            patch.object(workspace_membership.db, "get_workspace_master", return_value={"id": 1}),
            patch.object(workspace_membership.db, "get_workspace_candidate", return_value={
                "trash_status": "active", "trash_whitelist": 1,
            }),
            patch.object(workspace_membership.db, "get_workspace_settings", return_value={
                "trash_action": "seat",
            }),
            patch.object(
                workspace_membership, "_ensure_candidate_usage_based",
                return_value={"raw_seat_type": "usage_based"},
            ) as ensure,
            patch.object(workspace_membership.db, "update_workspace_candidate_trash", return_value=True),
            patch.object(workspace_membership, "_cleanup_cpa_credential_after_trash"),
        ):
            result = workspace_membership.trash_workspace_candidate(1, "m@example.com", reason="manual_trash")
        self.assertTrue(result["ok"])
        ensure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
