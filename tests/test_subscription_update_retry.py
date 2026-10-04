"""上游「Another subscription update is in progress」429 的专项处理。

这个 429 不是限流，是有在途席位变更：短退避没意义，固定等 30s 再重试。
覆盖：母号管理请求（_workspace_admin_request）和成员侧退出
（member_leave_workspace）两条链路；并锁定「前置动作失败绝不标记已入箱」。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db, workspace_membership


class _Resp:
    def __init__(self, status, body=""):
        self.status_code = status
        self.text = body

    def json(self):
        return {}


_SUB_BODY = '{"detail": "Another subscription update is in progress. Please try again."}'


class SubscriptionPendingDetectTests(unittest.TestCase):
    def test_matches_only_subscription_429(self):
        self.assertTrue(workspace_membership._is_subscription_update_pending(_Resp(429, _SUB_BODY)))
        self.assertFalse(workspace_membership._is_subscription_update_pending(_Resp(429, '{"detail":"Too many requests"}')))
        self.assertFalse(workspace_membership._is_subscription_update_pending(_Resp(200, _SUB_BODY)))
        self.assertFalse(workspace_membership._is_subscription_update_pending(_Resp(403, _SUB_BODY)))
        self.assertFalse(workspace_membership._is_subscription_update_pending(None))
        self.assertFalse(workspace_membership._is_subscription_update_pending(_Resp(429)))


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def delete(self, url, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


class MemberLeaveSubscriptionRetryTests(unittest.TestCase):
    def _run(self, responses):
        session = _FakeSession(responses)
        sleeps = []
        with (
            patch.object(
                workspace_membership.db, "get_workspace_master",
                return_value={"workspace_id": "ws-1"},
            ),
            patch.object(
                workspace_membership, "_candidate_quota_session",
                return_value=(session, {}),
            ),
            patch.object(workspace_membership.time, "sleep", side_effect=sleeps.append),
        ):
            result = workspace_membership.member_leave_workspace(
                1, "m@example.com", "mem-1", proxy="p",
            )
        return result, session, sleeps

    def test_subscription_429_waits_30s_and_retries(self):
        result, session, sleeps = self._run([
            _Resp(429, _SUB_BODY),
            _Resp(429, _SUB_BODY),
            _Resp(200, "{}"),
        ])
        self.assertTrue(result["left"])
        self.assertEqual(session.calls, 3)
        self.assertEqual(sleeps, [30.0, 30.0])

    def test_subscription_retry_budget_exhausted_still_fails(self):
        result, session, sleeps = self._run(
            [_Resp(429, _SUB_BODY)] * 5
        )
        self.assertFalse(result["left"])
        self.assertEqual(result["status_code"], 429)
        # 1 次首发 + 3 次订阅重试 = 4 次请求
        self.assertEqual(session.calls, 4)
        self.assertEqual(sleeps, [30.0] * 3)

    def test_generic_429_does_not_wait_or_retry(self):
        result, session, sleeps = self._run([
            _Resp(429, '{"detail":"Too many requests"}'),
        ])
        self.assertFalse(result["left"])
        self.assertEqual(session.calls, 1)
        self.assertEqual(sleeps, [])

    def test_404_still_resolves_member_id_once(self):
        session = _FakeSession([_Resp(404), _Resp(200, "{}")])
        with (
            patch.object(
                workspace_membership.db, "get_workspace_master",
                return_value={"workspace_id": "ws-1"},
            ),
            patch.object(
                workspace_membership, "_candidate_quota_session",
                return_value=(session, {}),
            ),
            patch.object(
                workspace_membership, "fetch_candidate_seats",
                return_value={"m@example.com": {"member_id": "mem-2"}},
            ),
            patch.object(workspace_membership.time, "sleep", side_effect=lambda s: None),
        ):
            result = workspace_membership.member_leave_workspace(
                1, "m@example.com", "mem-1", proxy="p",
            )
        self.assertTrue(result["left"])
        self.assertEqual(result["member_id"], "mem-2")


class AdminRequestSubscriptionRetryTests(unittest.TestCase):
    def test_admin_request_waits_30s_on_subscription_429(self):
        session = _FakeSession([
            _Resp(429, _SUB_BODY),
            _Resp(200, "{}"),
        ])
        sleeps = []
        with patch.object(
            workspace_membership.time, "sleep", side_effect=sleeps.append
        ):
            resp = workspace_membership._workspace_admin_request(
                999999, session, "delete", "https://example.test/x",
                request_interval=0,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 29.9)
        self.assertLessEqual(sleeps[0], 30.0)

    def test_admin_request_generic_429_uses_short_backoff(self):
        session = _FakeSession([
            _Resp(429, '{"detail":"Too many requests"}'),
            _Resp(200, "{}"),
        ])
        sleeps = []
        with patch.object(
            workspace_membership.time, "sleep", side_effect=sleeps.append
        ):
            resp = workspace_membership._workspace_admin_request(
                999998, session, "delete", "https://example.test/x",
                request_interval=0,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(sleeps), 1)
        self.assertLess(sleeps[0], 30.0)


class FailedPreActionNeverTrashesTests(unittest.TestCase):
    """前置动作未确认（含订阅在途重试耗尽）时，候选人绝不能落 trashed。"""

    def test_leave_failure_leaves_candidate_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                db.import_workspace_sessions(
                    "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
                    "----socks5://127.0.0.1:1080"
                )
                db.save_registered({"email": "m@example.com", "access_token": "at"})
                db.assign_workspace_candidates(1, ["m@example.com"])
                db.update_workspace_settings(1, {"trash_action": "leave"})
                with (
                    patch.object(
                        workspace_membership, "_lease_member_side_proxy",
                        return_value="proxy://x",
                    ),
                    patch.object(
                        workspace_membership, "member_leave_workspace",
                        return_value={
                            "member_id": "mem-1", "left": False,
                            "status_code": 429,
                            "error": "成员退出空间未被确认 HTTP 429",
                        },
                    ),
                ):
                    result = workspace_membership.trash_workspace_candidate(
                        1, "m@example.com", reason="quota_zero",
                    )
                    row = db.get_workspace_candidate(1, "m@example.com")
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending_seat"])
        self.assertEqual(row["trash_status"], "active")

    def test_kick_failure_leaves_candidate_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                db.import_workspace_sessions(
                    "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
                    "----socks5://127.0.0.1:1080"
                )
                db.save_registered({"email": "m@example.com", "access_token": "at"})
                db.assign_workspace_candidates(1, ["m@example.com"])
                db.update_workspace_settings(1, {"trash_action": "kick"})
                with patch.object(
                    workspace_membership, "remove_member",
                    return_value={
                        "member_id": "mem-1", "kicked": False,
                        "status_code": 429,
                        "error": "踢出成员未被确认 HTTP 429",
                    },
                ):
                    result = workspace_membership.trash_workspace_candidate(
                        1, "m@example.com", reason="quota_zero",
                    )
                    row = db.get_workspace_candidate(1, "m@example.com")
        self.assertFalse(result["ok"])
        self.assertTrue(result["pending_seat"])
        self.assertEqual(row["trash_status"], "active")


if __name__ == "__main__":
    unittest.main()
