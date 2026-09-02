import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from webui import app, db, workspace_membership


class CandidateStateConsistencyTests(unittest.TestCase):
    def test_credential_display_uses_actual_workspace_token_not_legacy_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.import_workspace_sessions(
                    "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
                    "----socks5://127.0.0.1:1080"
                )
                db.save_registered({"email": "member@example.com", "access_token": "personal-token"})
                db.assign_workspace_candidates(1, ["member@example.com"])
                con = sqlite3.connect(str(path))
                con.execute(
                    "UPDATE workspace_candidates SET status='workspace_credential' WHERE workspace_master_id=1"
                )
                con.commit()
                con.close()

                row = db.list_workspace_candidate_options(1)[0]
                workspace_rows = db.list_workspace_candidate_options(
                    1,
                    credential_status="workspace_credential",
                )

        self.assertEqual(row["has_workspace_access_token"], 0)
        self.assertEqual(row["credential_status"], "personal_credential")
        self.assertNotEqual(row["display_status"], "workspace_credential")
        self.assertEqual(workspace_rows, [])

    def test_invalid_candidate_is_listed_for_trash_reconciliation(self):
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
                db.mark_registered_permanently_invalid("member@example.com", "login 403")

                rows = db.list_invalid_workspace_candidates_pending_trash()

        self.assertEqual([(row["workspace_master_id"], row["email"]) for row in rows], [(1, "member@example.com")])

    def test_reconciliation_respects_each_workspace_setting(self):
        rows = [
            {"workspace_master_id": 1, "email": "enabled@example.com", "workspace_join_status": "not_invited"},
            {"workspace_master_id": 2, "email": "disabled@example.com", "workspace_join_status": "not_invited"},
        ]
        with (
            patch.object(app.db, "list_invalid_workspace_candidates_pending_trash", return_value=rows),
            patch.object(
                app.db,
                "get_workspace_settings",
                side_effect=lambda workspace_id: {"trash_invalid_enabled": workspace_id == 1},
            ),
            patch.object(app.db, "update_workspace_candidate_trash", return_value=True) as update,
            patch.object(app.workspace_membership, "trash_workspace_candidate") as switch_seat,
        ):
            result = app._reconcile_invalid_candidate_trash()

        self.assertEqual(result, {"scanned": 2, "marked": 0, "skipped": 2, "seat_pending": 0})
        update.assert_not_called()
        switch_seat.assert_not_called()

    def test_invalid_reconciliation_never_marks_before_codex_confirmation(self):
        rows = [{
            "workspace_master_id": 1,
            "email": "enabled@example.com",
            "member_id": "member-1",
            "workspace_join_status": "joined",
            "seat_type": "default",
        }]
        with (
            patch.object(app.db, "list_invalid_workspace_candidates_pending_trash", return_value=rows),
            patch.object(app.db, "get_workspace_settings", return_value={"trash_invalid_enabled": True}),
            patch.object(app.db, "update_workspace_candidate_trash") as update,
            patch.object(
                app.workspace_membership,
                "trash_workspace_candidate",
                return_value={"ok": False, "pending_seat": True},
            ) as trash,
        ):
            result = app._reconcile_invalid_candidate_trash()

        self.assertEqual(result, {"scanned": 1, "marked": 0, "skipped": 0, "seat_pending": 1})
        trash.assert_called_once_with(1, "enabled@example.com", reason="account_invalid", retries=1)
        update.assert_not_called()

    def test_immediate_invalid_trash_respects_each_workspace_setting(self):
        rows = [
            {"workspace_master_id": 1},
            {"workspace_master_id": 2},
        ]
        with (
            patch.object(workspace_membership.db, "list_workspace_candidates_by_email", return_value=rows),
            patch.object(
                workspace_membership.db,
                "get_workspace_settings",
                side_effect=lambda workspace_id: {"trash_invalid_enabled": workspace_id == 1},
            ),
            patch.object(
                workspace_membership,
                "trash_workspace_candidate",
                return_value={"ok": True, "seat": {"seat_type": "usage_based"}},
            ) as trash,
        ):
            result = workspace_membership.trash_workspace_candidates_by_email(
                "MEMBER@example.com",
                reason="login_403",
                respect_invalid_settings=True,
            )

        trash.assert_called_once_with(1, "member@example.com", reason="login_403", retries=3)
        self.assertEqual(result["trashed"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)

    def test_trash_requires_confirmed_usage_based_seat_before_marking(self):
        with (
            patch.object(workspace_membership.db, "get_workspace_master", return_value={"id": 1}),
            patch.object(workspace_membership.db, "get_workspace_candidate", return_value={}),
            patch.object(
                workspace_membership,
                "_ensure_candidate_usage_based",
                return_value={"raw_seat_type": "default"},
            ),
            patch.object(workspace_membership.db, "update_workspace_candidate_trash") as update,
        ):
            result = workspace_membership.trash_workspace_candidate(1, "member@example.com")

        self.assertFalse(result["ok"])
        self.assertTrue(result["pending_seat"])
        update.assert_not_called()

    def test_trash_does_not_call_upstream_when_mother_is_missing(self):
        with (
            patch.object(workspace_membership.db, "get_workspace_master", return_value=None),
            patch.object(workspace_membership, "_ensure_candidate_usage_based") as ensure,
        ):
            result = workspace_membership.trash_workspace_candidate(19, "member@example.com")

        self.assertFalse(result["ok"])
        self.assertFalse(result["pending_seat"])
        self.assertIn("母号不存在", result["error"])
        ensure.assert_not_called()

    def test_trash_marks_only_after_usage_based_confirmation(self):
        with (
            patch.object(workspace_membership.db, "get_workspace_master", return_value={"id": 1}),
            patch.object(workspace_membership.db, "get_workspace_candidate", return_value={}),
            patch.object(
                workspace_membership,
                "_ensure_candidate_usage_based",
                return_value={"raw_seat_type": "usage_based"},
            ),
            patch.object(workspace_membership.db, "update_workspace_candidate_trash") as update,
        ):
            result = workspace_membership.trash_workspace_candidate(1, "member@example.com")

        self.assertTrue(result["ok"])
        update.assert_called_once_with(
            1,
            "member@example.com",
            status="trashed",
            reason="manual",
            due_at=0,
        )

    def test_quota_eligibility_distinguishes_credentials_from_seat_type(self):
        base = {
            "account_status": "active",
            "trash_status": "active",
            "has_workspace_access_token": 1,
        }
        self.assertEqual(app._candidate_quota_ineligible_reason({**base, "seat_type": "default"}), "")
        self.assertEqual(
            app._candidate_quota_ineligible_reason({**base, "seat_type": "usage_based"}),
            "Codex席位不参与额度查询",
        )
        self.assertEqual(
            app._candidate_quota_ineligible_reason({**base, "has_workspace_access_token": 0}),
            "未获得当前空间凭证",
        )

    def test_quota_api_returns_explicit_result_for_ineligible_candidate(self):
        request = app.WorkspaceCandidatesReq(
            workspace_id=5,
            emails=["candidate@example.com"],
            proxy_pool="proxy-one",
        )
        rows = [{
            "email": "candidate@example.com",
            "account_status": "active",
            "trash_status": "active",
            "has_workspace_access_token": 1,
            "seat_type": "usage_based",
        }]
        with (
            patch.object(app, "_reject_trashed_candidates"),
            patch.object(app.db, "get_workspace_settings", return_value={}),
            patch.object(app.db, "list_workspace_candidate_options", return_value=rows),
            patch.object(app.workspace_membership, "fetch_candidate_quota") as fetch,
        ):
            result = app.api_workspace_candidate_quota(request)

        self.assertEqual(
            result["results"]["candidate@example.com"],
            {"ok": False, "error": "Codex席位不参与额度查询", "skipped": True},
        )
        fetch.assert_not_called()


    def test_increment_workspace_fulfillment_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.import_workspace_sessions(
                    "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
                    "----socks5://127.0.0.1:1080"
                )
                c1 = db.increment_workspace_fulfillment_counter(1, "prolite", 1)
                c2 = db.increment_workspace_fulfillment_counter(1, "prolite", 2)
                c3 = db.increment_workspace_fulfillment_counter(1, "default", 1)

                stats = db.get_workspace_candidate_stats(1)

        self.assertEqual(c1, 1)
        self.assertEqual(c2, 3)
        self.assertEqual(c3, 1)
        self.assertEqual(stats["seat_fulfillment"]["prolite"]["fulfilled_total"], 3)
        self.assertEqual(stats["seat_fulfillment"]["standard"]["fulfilled_total"], 1)


if __name__ == "__main__":
    unittest.main()
