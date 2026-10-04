"""对外入箱接口：POST /api/workspace-candidates/trash-by-workspace。

外部程序按 OpenAI workspace_id + 邮箱唯一定位成员；覆盖外部 ID 映射、
多母号同空间、404 分支、前置动作失败不落箱。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from webui import app, db, workspace_membership


def _seed(external_id="ws-ext-1", extra_master=False):
    db.init_db()
    db.import_workspace_sessions(
        "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
        "----socks5://127.0.0.1:1080"
    )
    db.save_registered({"email": "m@example.com", "access_token": "at"})
    db.assign_workspace_candidates(1, ["m@example.com"])
    con = db._conn()
    con.execute("UPDATE workspace_masters SET workspace_id=? WHERE id=1", (external_id,))
    if extra_master:
        con.execute(
            "INSERT INTO workspace_masters(account,email,workspace_id,access_token,session_token,proxy_url,status,imported_at,updated_at) "
            "VALUES('m2','o2@x.com',?,?,?,?, 'ok', 0, 0)",
            (external_id, "a2", "s2", ""),
        )
        con.commit()
        db.assign_workspace_candidates(2, ["m@example.com"])
    con.commit()


class ExternalTrashApiTests(unittest.TestCase):
    def test_trashes_by_external_workspace_id_and_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed()
                with patch.object(
                    workspace_membership, "trash_workspace_candidate",
                    return_value={"ok": True, "action": "seat"},
                ) as trash:
                    resp = app.api_trash_member_by_workspace(
                        app.ExternalTrashMemberReq(
                            workspace_id="ws-ext-1", email="m@example.com"
                        )
                    )
        trash.assert_called_once_with(1, "m@example.com", reason="api_trash")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["trashed"], 1)
        self.assertEqual(resp["results"][0]["workspace_db_id"], 1)

    def test_unknown_workspace_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed()
                with self.assertRaises(HTTPException) as ctx:
                    app.api_trash_member_by_workspace(
                        app.ExternalTrashMemberReq(
                            workspace_id="ws-unknown", email="m@example.com"
                        )
                    )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_unknown_candidate_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed()
                with self.assertRaises(HTTPException) as ctx:
                    app.api_trash_member_by_workspace(
                        app.ExternalTrashMemberReq(
                            workspace_id="ws-ext-1", email="ghost@example.com"
                        )
                    )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_all_local_masters_sharing_external_id_get_trashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed(extra_master=True)
                calls = []

                def fake_trash(wid, email, reason=""):
                    calls.append((wid, email, reason))
                    return {"ok": True, "action": "seat"}

                with patch.object(
                    workspace_membership, "trash_workspace_candidate",
                    side_effect=fake_trash,
                ):
                    resp = app.api_trash_member_by_workspace(
                        app.ExternalTrashMemberReq(
                            workspace_id="ws-ext-1", email="m@example.com"
                        )
                    )
        self.assertTrue(resp["ok"])
        self.assertEqual(
            sorted(calls), [(1, "m@example.com", "api_trash"), (2, "m@example.com", "api_trash")]
        )

    def test_pending_seat_result_not_marked_trashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed()
                with patch.object(
                    workspace_membership, "trash_workspace_candidate",
                    return_value={
                        "ok": False, "pending_seat": True,
                        "action": "leave",
                        "error": "成员退出空间未被确认 HTTP 429",
                    },
                ):
                    resp = app.api_trash_member_by_workspace(
                        app.ExternalTrashMemberReq(
                            workspace_id="ws-ext-1", email="m@example.com"
                        )
                    )
                    row = db.get_workspace_candidate(1, "m@example.com")
        self.assertFalse(resp["ok"])
        self.assertTrue(resp["results"][0]["pending_seat"])
        self.assertEqual(row["trash_status"], "active")

    def test_api_trash_reason_counts_as_manual_for_cpa_cleanup(self):
        self.assertIn("api_trash", workspace_membership._MANUAL_TRASH_REASONS)

    def test_blank_fields_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            app.api_trash_member_by_workspace(
                app.ExternalTrashMemberReq(workspace_id=" ", email="m@example.com")
            )
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
