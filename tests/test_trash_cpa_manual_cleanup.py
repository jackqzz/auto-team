"""手动入箱联动删 CPA 凭证的空间开关（trash_cleanup_cpa_on_manual）。

锁定口径：
- 自动化入箱（quota_zero 等）始终删 CPA + 释放代理，不受开关影响；
- 手动入箱（manual_trash）、手动踢出（kicked）、手动退出（left_workspace）
  只在空间开关打开时才删 CPA / 释放代理；
- /kick、/leave 两个手动端点成功后确实走到了清理函数。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db, workspace_membership


def _seed_workspace():
    db.init_db()
    db.import_workspace_sessions(
        "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
        "----socks5://127.0.0.1:1080"
    )
    db.save_registered({"email": "m@example.com", "access_token": "at"})
    db.assign_workspace_candidates(1, ["m@example.com"])


def _run_cleanup(reason):
    with (
        patch("webui.exporter.delete_cpa_auth_file") as delete,
        patch.object(db, "release_cpa_proxy", return_value="") as release,
    ):
        workspace_membership._cleanup_cpa_credential_after_trash(1, "m@example.com", reason)
    return delete, release


class ManualCleanupGateTests(unittest.TestCase):
    def _ws(self, tmp, settings=None):
        with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
            _seed_workspace()
            updates = {
                "auto_push_cpa_url": "https://cpa.local",
                "auto_push_cpa_mgmt_key": "k",
            }
            if settings:
                updates.update(settings)
            db.update_workspace_settings(1, updates)
            yield

    def test_auto_reason_always_deletes_even_when_toggle_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in self._ws(tmp, {"trash_cleanup_cpa_on_manual": False}):
                delete, release = _run_cleanup("quota_zero")
        delete.assert_called_once()
        release.assert_called_once_with(1, "m@example.com")

    def test_manual_trash_skipped_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in self._ws(tmp):
                delete, release = _run_cleanup("manual_trash")
        delete.assert_not_called()
        release.assert_not_called()

    def test_manual_reasons_delete_when_toggle_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in self._ws(tmp, {"trash_cleanup_cpa_on_manual": True}):
                for reason in ("manual_trash", "kicked", "left_workspace", "manual"):
                    with self.subTest(reason=reason):
                        delete, release = _run_cleanup(reason)
                        delete.assert_called_once()
                        release.assert_called_once_with(1, "m@example.com")

    def test_unknown_reason_never_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in self._ws(tmp, {"trash_cleanup_cpa_on_manual": True}):
                delete, release = _run_cleanup("seat")
                delete2, release2 = _run_cleanup("")
        delete.assert_not_called()
        release.assert_not_called()
        delete2.assert_not_called()
        release2.assert_not_called()


class ManualEndpointsCleanupHookTests(unittest.TestCase):
    """手动 /kick、/leave 端点在远端确认后必须走到 CPA 清理函数。"""

    def test_kick_endpoint_invokes_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed_workspace()
                db.update_workspace_candidate_member(1, "m@example.com", "mem-1", "default")
                with (
                    patch.object(
                        workspace_membership, "remove_member",
                        return_value={"kicked": True},
                    ),
                    patch.object(
                        workspace_membership, "_cleanup_cpa_credential_after_trash"
                    ) as cleanup,
                ):
                    resp = app.api_kick_workspace_candidates(
                        app.WorkspaceCandidatesReq(workspace_id=1, emails=["m@example.com"])
                    )
        self.assertEqual(resp["kicked"], 1)
        cleanup.assert_called_once_with(1, "m@example.com", "kicked")

    def test_leave_endpoint_invokes_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed_workspace()
                db.update_workspace_candidate_member(1, "m@example.com", "mem-1", "default")
                with (
                    patch.object(
                        app, "_candidate_quota_proxy_pool",
                        return_value=type("L", (), {"lease": staticmethod(lambda *a, **k: ("p", 0, 1))})(),
                    ),
                    patch.object(
                        app, "_lease_candidate_quota_proxy", return_value="proxy://x"
                    ),
                    patch.object(
                        workspace_membership, "member_leave_workspace",
                        return_value={"left": True},
                    ),
                    patch.object(
                        workspace_membership, "_cleanup_cpa_credential_after_trash"
                    ) as cleanup,
                ):
                    resp = app.api_member_leave_workspace(
                        app.WorkspaceCandidatesReq(workspace_id=1, emails=["m@example.com"])
                    )
        self.assertEqual(resp["left"], 1)
        cleanup.assert_called_once_with(1, "m@example.com", "left_workspace")

    def test_failed_kick_does_not_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                _seed_workspace()
                with (
                    patch.object(
                        workspace_membership, "remove_member",
                        return_value={"kicked": False, "error": "forbidden"},
                    ),
                    patch.object(
                        workspace_membership, "_cleanup_cpa_credential_after_trash"
                    ) as cleanup,
                ):
                    app.api_kick_workspace_candidates(
                        app.WorkspaceCandidatesReq(workspace_id=1, emails=["m@example.com"])
                    )
        cleanup.assert_not_called()


class PersonalManualCleanupTests(unittest.TestCase):
    """个人空间沿用同一开关：默认开（手动入箱删 CPA），关掉就不删。"""

    def _apply(self, reason, settings=None):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                db.save_registered({"email": "a@example.com", "access_token": "x"})
                db.assign_personal_candidates(["a@example.com"])
                db.update_personal_settings({
                    "auto_push_cpa_url": "https://cpa.local",
                    "auto_push_cpa_mgmt_key": "k",
                    **(settings or {}),
                })
                with patch("webui.exporter.delete_cpa_auth_file") as delete:
                    delete.return_value = {"ok": True, "deleted": True}
                    app._apply_personal_trash("a@example.com", reason=reason)
                    return delete

    def test_manual_reason_deletes_by_default(self):
        delete = self._apply("manual")
        delete.assert_called_once()

    def test_manual_reason_skips_when_disabled(self):
        delete = self._apply("manual", {"trash_cleanup_cpa_on_manual": False})
        delete.assert_not_called()

    def test_auto_reason_deletes_even_when_disabled(self):
        delete = self._apply("quota_zero", {"trash_cleanup_cpa_on_manual": False})
        delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
