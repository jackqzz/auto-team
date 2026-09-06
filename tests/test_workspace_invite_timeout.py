import unittest
from unittest.mock import Mock, patch

from webui import workspace_membership


def _master():
    return {"workspace_id": "workspace-1", "access_token": "master-token"}


class InviteTimeoutTests(unittest.TestCase):
    """母号邀请的网络超时必须够长。

    上游批量邀请在人多时经常要 30s 以上才返回，原来的 30s 下限会在结果回来之前
    掐断连接：邀请其实已经发出，前端却只看到"超时"，用户重复点击造成重复邀请。
    """

    def _invite(self, count):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True}
        session = Mock()
        session.post.return_value = response
        emails = [f"user{i}@example.com" for i in range(count)]

        with patch.object(
            workspace_membership, "create_workspace_http_session",
            return_value=(session, _master()),
        ):
            workspace_membership.invite_candidates(1, emails)

        return session.post.call_args.kwargs["timeout"]

    def test_invite_timeout_never_below_shared_floor(self):
        floor = workspace_membership.WORKSPACE_ADMIN_REQUEST_TIMEOUT_SECONDS
        self.assertGreaterEqual(floor, 60)
        for count in (1, 2, 5, 10, 20):
            with self.subTest(count=count):
                self.assertGreaterEqual(self._invite(count), floor)

    def test_invite_timeout_still_scales_with_batch_size_and_is_capped(self):
        # 大批量仍然要按人数增长，但不能无上限
        self.assertEqual(self._invite(100), 220)
        self.assertEqual(self._invite(500), 300)

    def test_membership_recheck_requests_use_the_same_floor(self):
        """复查跑在同一个邀请请求里，它卡 30s 会让整体一样早断。"""
        floor = workspace_membership.WORKSPACE_ADMIN_REQUEST_TIMEOUT_SECONDS
        invites_response = Mock(status_code=200)
        invites_response.json.return_value = {"items": [], "total": 0}
        users_response = Mock(status_code=200)
        users_response.json.return_value = {"items": []}
        session = Mock()
        session.get.side_effect = lambda url, **kw: (
            invites_response if "/invites" in url else users_response
        )

        with patch.object(
            workspace_membership, "create_workspace_http_session",
            return_value=(session, _master()),
        ):
            workspace_membership.check_candidate_membership(
                1, ["user@example.com"], prefer_invites=True, include_seats=True,
            )

        timeouts = [c.kwargs.get("timeout") for c in session.get.call_args_list]
        self.assertTrue(timeouts, "复查应当发出请求")
        for value in timeouts:
            self.assertGreaterEqual(value, floor)


if __name__ == "__main__":
    unittest.main()
