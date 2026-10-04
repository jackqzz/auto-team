"""垃圾箱入箱前置动作可切换：席位切为 Codex（seat，默认）/ 母号踢出空间（kick）/ 成员主动退出（leave）。

锁定三件事：模式读自空间设置且非法值回落 seat；kick/leave 模式只在远端确认
（kicked/left/already_gone）后才落库，未确认时给 pending_seat 交给调用方重排；
seat 模式行为保持不变。
"""
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from webui import app, db, workspace_membership


class TrashActionSettingTests(unittest.TestCase):
    def test_defaults_to_seat_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                db.init_db()
                db.import_workspace_sessions(
                    "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
                    "----socks5://127.0.0.1:1080"
                )
                self.assertEqual(db.get_workspace_settings(1)["trash_action"], "seat")
                db.update_workspace_settings(1, {"trash_action": "kick"})
                self.assertEqual(db.get_workspace_settings(1)["trash_action"], "kick")
                stats = db.get_workspace_candidate_stats(1)
        self.assertEqual(stats["trash"]["trash_action"], "kick")

    def test_normalize_only_accepts_kick_and_leave(self):
        self.assertEqual(app._normalize_trash_action("kick"), "kick")
        self.assertEqual(app._normalize_trash_action(" KICK "), "kick")
        self.assertEqual(app._normalize_trash_action("leave"), "leave")
        self.assertEqual(app._normalize_trash_action(" Leave "), "leave")
        for bad in ("seat", "", None, "remove", "bogus"):
            self.assertEqual(app._normalize_trash_action(bad), "seat")

    def test_candidate_trash_action_reads_workspace_settings(self):
        cases = [
            ({}, "seat"),
            ({"trash_action": "kick"}, "kick"),
            ({"trash_action": " Kick "}, "kick"),
            ({"trash_action": "leave"}, "leave"),
            ({"trash_action": " Leave "}, "leave"),
            ({"trash_action": "bogus"}, "seat"),
            ({"trash_action": None}, "seat"),
        ]
        for settings, expected in cases:
            with patch.object(
                workspace_membership.db, "get_workspace_settings", return_value=settings
            ):
                self.assertEqual(
                    workspace_membership._candidate_trash_action(1), expected,
                    msg=f"settings={settings}",
                )
        with patch.object(
            workspace_membership.db, "get_workspace_settings", side_effect=RuntimeError("db gone")
        ):
            self.assertEqual(workspace_membership._candidate_trash_action(1), "seat")


@contextmanager
def _trash_ctx(
    *, settings, row=None,
    remove_result=None, remove_side_effect=None,
    leave_result=None, leave_side_effect=None,
):
    """patch 掉 trash_workspace_candidate 的 DB/远端依赖，产出可断言的 mock 组。"""
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            workspace_membership.db, "get_workspace_master", return_value={"id": 1},
        ))
        stack.enter_context(patch.object(
            workspace_membership.db, "get_workspace_candidate",
            return_value=dict(row or {}),
        ))
        stack.enter_context(patch.object(
            workspace_membership.db, "get_workspace_settings", return_value=settings,
        ))
        mocks = {
            "ensure": stack.enter_context(patch.object(
                workspace_membership, "_ensure_candidate_usage_based",
                return_value={"raw_seat_type": "usage_based"},
            )),
            "remove": stack.enter_context(patch.object(
                workspace_membership, "remove_member",
                return_value=remove_result, side_effect=remove_side_effect,
            )),
            "leave": stack.enter_context(patch.object(
                workspace_membership, "member_leave_workspace",
                return_value=leave_result, side_effect=leave_side_effect,
            )),
            "lease": stack.enter_context(patch.object(
                workspace_membership, "_lease_member_side_proxy",
                return_value="socks5://test-proxy",
            )),
            "mark": stack.enter_context(patch.object(
                workspace_membership.db, "mark_workspace_candidates_kicked",
                return_value={"candidates": 1},
            )),
            "cleanup": stack.enter_context(patch.object(
                workspace_membership, "_cleanup_cpa_credential_after_trash",
            )),
            "update": stack.enter_context(patch.object(
                workspace_membership.db, "update_workspace_candidate_trash",
                return_value=True,
            )),
        }
        yield mocks


KICK_ROW = {"member_id": "member-1", "workspace_join_status": "joined"}


class KickModeTrashTests(unittest.TestCase):
    """trash_action=kick 时走 remove_member，不走席位切换。"""

    def test_kick_mode_calls_remove_member_and_marks_kicked(self):
        with _trash_ctx(
            settings={"trash_action": "kick"},
            row=KICK_ROW,
            remove_result={"member_id": "member-1", "kicked": True},
        ) as m:
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", reason="quota_zero",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "kick")
        m["remove"].assert_called_once_with(1, "member@example.com", "member-1")
        m["ensure"].assert_not_called()
        # 自动入箱保留真实触发原因而不是一律记成 kicked。
        m["mark"].assert_called_once_with(1, ["member@example.com"], reason="quota_zero")
        m["update"].assert_not_called()
        m["cleanup"].assert_called_once_with(1, "member@example.com", "quota_zero")

    def test_kick_mode_accepts_already_gone(self):
        with _trash_ctx(
            settings={"trash_action": "kick"},
            row=KICK_ROW,
            remove_result={"member_id": "", "kicked": False, "already_gone": True},
        ) as m:
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", reason="account_invalid",
            )
        self.assertTrue(result["ok"])
        m["mark"].assert_called_once_with(1, ["member@example.com"], reason="account_invalid")

    def test_kick_mode_finds_member_id_when_row_missing(self):
        """快照里没有 member_id 时传空串，由 remove_member 回查成员列表。"""
        with _trash_ctx(
            settings={"trash_action": "kick"},
            row={},
            remove_result={"member_id": "m-fresh", "kicked": True},
        ) as m:
            result = workspace_membership.trash_workspace_candidate(1, "member@example.com")
        self.assertTrue(result["ok"])
        m["remove"].assert_called_once_with(1, "member@example.com", "")

    def test_kick_failure_returns_pending_without_marking(self):
        with _trash_ctx(
            settings={"trash_action": "kick"},
            row=KICK_ROW,
            remove_side_effect=RuntimeError("network down"),
        ) as m:
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", reason="quota_zero", retries=1,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending_seat"])
        self.assertEqual(result["action"], "kick")
        m["mark"].assert_not_called()
        m["update"].assert_not_called()
        m["cleanup"].assert_not_called()

    def test_unconfirmed_kick_returns_pending_without_marking(self):
        """DELETE 被拒绝（如母号无踢人权限）时绝不能标已入箱。"""
        with _trash_ctx(
            settings={"trash_action": "kick"},
            row=KICK_ROW,
            remove_result={
                "member_id": "member-1",
                "kicked": False,
                "status_code": 403,
                "error": "踢出成员未被确认 HTTP 403",
            },
        ) as m:
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", reason="quota_zero",
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending_seat"])
        m["mark"].assert_not_called()
        m["update"].assert_not_called()

    def test_kick_retry_after_exception_uses_fresh_member_id(self):
        """失败后重试时清空过期 member_id，让 remove_member 重新解析。"""
        calls = []

        def flaky(wid, email, member_id):
            calls.append(member_id)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"member_id": "m-new", "kicked": True}

        with _trash_ctx(settings={"trash_action": "kick"}, row=KICK_ROW) as m:
            m["remove"].side_effect = flaky
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", retries=2,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["member-1", ""])
        m["mark"].assert_called_once()


class SeatModeUnchangedTests(unittest.TestCase):
    """seat 模式（默认）保持既有语义：先切 usage_based 再入箱。"""

    def test_seat_mode_does_not_call_remove_member(self):
        with _trash_ctx(settings={"trash_action": "seat"}, row={}) as m:
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", reason="quota_zero",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "seat")
        m["ensure"].assert_called_once()
        m["remove"].assert_not_called()
        m["update"].assert_called_once_with(
            1, "member@example.com", status="trashed", reason="quota_zero", due_at=0,
        )
        m["cleanup"].assert_called_once_with(1, "member@example.com", "quota_zero")
        m["mark"].assert_not_called()

    def test_invalid_mode_falls_back_to_seat(self):
        with _trash_ctx(settings={"trash_action": "definitely-not-a-mode"}, row={}) as m:
            result = workspace_membership.trash_workspace_candidate(1, "member@example.com")
        self.assertTrue(result["ok"])
        m["ensure"].assert_called_once()
        m["remove"].assert_not_called()


class ApplyCandidateTrashPendingTests(unittest.TestCase):
    def test_pending_kick_is_rescheduled_like_pending_seat(self):
        with (
            patch.object(
                app.workspace_membership,
                "trash_workspace_candidate",
                return_value={"ok": False, "pending_seat": True, "action": "kick"},
            ),
            patch.object(app.db, "update_workspace_candidate_trash", return_value=True) as update,
        ):
            result = app._apply_candidate_trash(1, "member@example.com", reason="quota_zero")
        self.assertFalse(result["ok"])
        update.assert_called_once()
        kwargs = update.call_args.kwargs
        self.assertEqual(kwargs["status"], "scheduled")
        self.assertEqual(kwargs["reason"], "seat_retry")


class MarkKickedReasonTests(unittest.TestCase):
    def _workspace_with_candidate(self):
        db.init_db()
        db.import_workspace_sessions(
            "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
            "----socks5://127.0.0.1:1080"
        )
        db.save_registered({"email": "member@example.com", "access_token": "token"})
        db.assign_workspace_candidates(1, ["member@example.com"])

    def test_default_reason_is_kicked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._workspace_with_candidate()
                db.mark_workspace_candidates_kicked(1, ["member@example.com"])
                row = db.get_workspace_candidate(1, "member@example.com")
        self.assertEqual(row["trash_status"], "trashed")
        self.assertEqual(row["trash_reason"], "kicked")

    def test_custom_reason_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._workspace_with_candidate()
                db.mark_workspace_candidates_kicked(
                    1, ["member@example.com"], reason="quota_zero",
                )
                row = db.get_workspace_candidate(1, "member@example.com")
        self.assertEqual(row["trash_status"], "trashed")
        self.assertEqual(row["trash_reason"], "quota_zero")
        self.assertEqual(row["member_id"], "")
        self.assertEqual(row["workspace_join_status"], "not_invited")


class RemoveMemberSemanticsTests(unittest.TestCase):
    """remove_member 只在 DELETE 返回 2xx 时才确认 kicked。"""

    class _Resp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    def _run(self, responses, member_id="member-1", seats=None):
        session = object()
        responses = iter(responses)
        with (
            patch.object(
                workspace_membership,
                "create_workspace_http_session",
                return_value=(session, {"workspace_id": "ws-1", "access_token": "tok"}),
            ),
            patch.object(
                workspace_membership,
                "fetch_candidate_seats",
                return_value=seats or {},
            ) as fetch_seats,
            patch.object(
                workspace_membership,
                "_workspace_admin_request",
                side_effect=lambda *a, **k: next(responses),
            ) as request,
        ):
            result = workspace_membership.remove_member(1, "member@example.com", member_id)
        return result, request, fetch_seats

    def test_two_xx_confirms_kick(self):
        result, _, _ = self._run([self._Resp(200, {"ok": True})])
        self.assertTrue(result["kicked"])
        self.assertFalse(result.get("already_gone", False))

    def test_non_2xx_is_not_a_kick(self):
        result, _, _ = self._run([self._Resp(403, {"message": "forbidden"})])
        self.assertFalse(result["kicked"])
        self.assertFalse(result.get("already_gone", False))
        self.assertEqual(result["status_code"], 403)
        self.assertIn("403", result["error"])

    def test_404_re_resolves_member_id_once(self):
        seats = {"member@example.com": {"member_id": "member-fresh"}}
        result, request, _ = self._run(
            [self._Resp(404), self._Resp(200)], member_id="member-stale", seats=seats,
        )
        self.assertTrue(result["kicked"])
        self.assertEqual(result["member_id"], "member-fresh")
        urls = [str(call.args[3]) for call in request.call_args_list]
        self.assertIn("member-stale", urls[0])
        self.assertIn("member-fresh", urls[1])

    def test_404_with_no_fresh_member_is_already_gone(self):
        result, request, _ = self._run([self._Resp(404)], member_id="member-stale")
        self.assertTrue(result["already_gone"])
        self.assertFalse(result["kicked"])
        self.assertEqual(request.call_count, 1)

    def test_missing_member_id_resolves_via_member_list(self):
        seats = {"member@example.com": {"member_id": "member-x"}}
        result, request, fetch_seats = self._run(
            [self._Resp(200)], member_id="", seats=seats,
        )
        self.assertTrue(result["kicked"])
        self.assertEqual(result["member_id"], "member-x")
        fetch_seats.assert_called_once()

    def test_missing_member_id_and_not_in_list_is_already_gone(self):
        result, request, _ = self._run([], member_id="")
        self.assertTrue(result["already_gone"])
        self.assertFalse(result["kicked"])
        request.assert_not_called()


class LeaveModeTrashTests(unittest.TestCase):
    """trash_action=leave 时走成员侧 member_leave_workspace，不走母号踢人/席位切换。"""

    def test_leave_mode_uses_member_side_call_and_marks(self):
        with _trash_ctx(
            settings={"trash_action": "leave"},
            row=KICK_ROW,
            leave_result={"member_id": "member-1", "left": True},
        ) as m:
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", reason="quota_zero",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "leave")
        m["lease"].assert_called_once_with(
            1, "member@example.com", detail="trash_member_leave",
        )
        m["leave"].assert_called_once_with(
            1, "member@example.com", "member-1", proxy="socks5://test-proxy",
        )
        m["remove"].assert_not_called()
        m["ensure"].assert_not_called()
        # 自动入箱保留真实触发原因而不是一律记成 left_workspace。
        m["mark"].assert_called_once_with(1, ["member@example.com"], reason="quota_zero")
        m["update"].assert_not_called()
        m["cleanup"].assert_called_once_with(1, "member@example.com", "quota_zero")

    def test_leave_mode_accepts_already_gone(self):
        with _trash_ctx(
            settings={"trash_action": "leave"},
            row={},
            leave_result={"member_id": "", "left": False, "already_gone": True},
        ) as m:
            result = workspace_membership.trash_workspace_candidate(1, "member@example.com")
        self.assertTrue(result["ok"])
        m["leave"].assert_called_once_with(1, "member@example.com", "", proxy="socks5://test-proxy")
        m["mark"].assert_called_once_with(1, ["member@example.com"], reason="left_workspace")

    def test_leave_failure_returns_pending_without_marking(self):
        with _trash_ctx(
            settings={"trash_action": "leave"},
            row=KICK_ROW,
            leave_side_effect=RuntimeError("network down"),
        ) as m:
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", reason="quota_zero", retries=1,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending_seat"])
        self.assertEqual(result["action"], "leave")
        m["mark"].assert_not_called()
        m["update"].assert_not_called()
        m["cleanup"].assert_not_called()

    def test_unconfirmed_leave_returns_pending_without_marking(self):
        """成员侧 DELETE 被拒绝（如凭证失效）时绝不能标已入箱。"""
        with _trash_ctx(
            settings={"trash_action": "leave"},
            row=KICK_ROW,
            leave_result={
                "member_id": "member-1",
                "left": False,
                "status_code": 403,
                "error": "成员退出空间未被确认 HTTP 403",
            },
        ) as m:
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", reason="quota_zero",
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending_seat"])
        m["mark"].assert_not_called()
        m["update"].assert_not_called()

    def test_leave_proxy_lease_failure_is_pending(self):
        """候选人代理池为空时绝不回落母号出口：租不到代理即失败转重试。"""
        with _trash_ctx(settings={"trash_action": "leave"}, row=KICK_ROW) as m:
            m["lease"].side_effect = ValueError("候选人代理池为空，成员退出空间无法租取代理")
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", retries=1,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending_seat"])
        m["leave"].assert_not_called()
        m["mark"].assert_not_called()

    def test_leave_retry_after_exception_uses_fresh_member_id(self):
        """失败后重试时清空过期 member_id，让 member_leave_workspace 重新解析。"""
        calls = []

        def flaky(wid, email, member_id, *, proxy):
            calls.append(member_id)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"member_id": "m-new", "left": True}

        with _trash_ctx(settings={"trash_action": "leave"}, row=KICK_ROW) as m:
            m["leave"].side_effect = flaky
            result = workspace_membership.trash_workspace_candidate(
                1, "member@example.com", retries=2,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["member-1", ""])
        m["mark"].assert_called_once()


class MemberLeaveSemanticsTests(unittest.TestCase):
    """member_leave_workspace 用成员凭证打同一个 DELETE 端点，只在 2xx 时确认 left。"""

    class _Resp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    class _Session:
        def __init__(self, responses):
            self._responses = iter(responses)
            self.calls = []

        def delete(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return next(self._responses)

    def _run(self, responses, member_id="member-1", seats=None):
        session = self._Session(responses)
        with (
            patch.object(
                workspace_membership.db, "get_workspace_master",
                return_value={"workspace_id": "ws-1"},
            ),
            patch.object(
                workspace_membership, "_candidate_quota_session",
                return_value=(
                    session,
                    {"Authorization": "Bearer member-tok", "ChatGPT-Account-Id": "ws-1"},
                ),
            ) as quota_session,
            patch.object(
                workspace_membership, "fetch_candidate_seats",
                return_value=seats or {},
            ) as fetch_seats,
        ):
            result = workspace_membership.member_leave_workspace(
                1, "member@example.com", member_id, proxy="socks5://p",
            )
        return result, session, quota_session, fetch_seats

    def test_two_xx_confirms_leave_with_member_headers(self):
        result, session, quota_session, _ = self._run([self._Resp(200, {"success": True})])
        self.assertTrue(result["left"])
        url, kwargs = session.calls[0]
        self.assertIn("/backend-api/accounts/ws-1/users/member-1", url)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer member-tok")
        self.assertEqual(kwargs["headers"]["ChatGPT-Account-Id"], "ws-1")
        quota_session.assert_called_once_with(1, "member@example.com", "socks5://p")

    def test_non_2xx_is_not_a_leave(self):
        result, session, _, _ = self._run([self._Resp(403, {"message": "forbidden"})])
        self.assertFalse(result["left"])
        self.assertFalse(result.get("already_gone", False))
        self.assertEqual(result["status_code"], 403)
        self.assertIn("403", result["error"])

    def test_404_re_resolves_member_id_once(self):
        seats = {"member@example.com": {"member_id": "member-fresh"}}
        result, session, _, _ = self._run(
            [self._Resp(404), self._Resp(200)], member_id="member-stale", seats=seats,
        )
        self.assertTrue(result["left"])
        self.assertEqual(result["member_id"], "member-fresh")
        self.assertIn("member-stale", session.calls[0][0])
        self.assertIn("member-fresh", session.calls[1][0])

    def test_404_with_no_fresh_member_is_already_gone(self):
        result, session, _, _ = self._run([self._Resp(404)], member_id="member-stale")
        self.assertTrue(result["already_gone"])
        self.assertFalse(result["left"])
        self.assertEqual(len(session.calls), 1)

    def test_missing_member_id_resolves_via_member_list(self):
        seats = {"member@example.com": {"member_id": "member-x"}}
        result, session, _, fetch_seats = self._run(
            [self._Resp(200)], member_id="", seats=seats,
        )
        self.assertTrue(result["left"])
        self.assertEqual(result["member_id"], "member-x")
        fetch_seats.assert_called_once()

    def test_missing_member_id_and_not_in_list_is_already_gone(self):
        result, session, quota_session, _ = self._run([], member_id="")
        self.assertTrue(result["already_gone"])
        self.assertFalse(result["left"])
        quota_session.assert_not_called()
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
