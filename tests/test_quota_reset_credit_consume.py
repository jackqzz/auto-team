"""额度重置券的手动兑换通道。

券是不可逆的消耗品：上游只要返回 2xx，券就扣掉了，哪怕只重置了部分窗口。
所以这里锁住的重点不是"能不能兑"，而是"不会多兑"：

* 兑换前必须先列券并按 id 指定，不能盲发；
* 失败绝不自动重试（重试 = 可能烧掉第二张券）；
* 兑换成功后的额度重查失败，不能把整个请求报成失败，否则用户会再点一次。

用例里的 payload 形状取自线上真实响应。
"""
import unittest
from unittest.mock import Mock, patch

from webui import app, workspace_membership


def _listing_payload(status="available", credit_id="RateLimitResetCredit_abc"):
    """线上 GET /wham/rate-limit-reset-credits 的真实响应形状。"""
    return {
        "credits": [{
            "id": credit_id,
            "reset_type": "codex_rate_limits",
            "is_supported_by_plan": True,
            "status": status,
            "granted_at": "2026-09-05T04:21:34.111065Z",
            "expires_at": "2026-10-05T04:21:34.111065Z",
            "redeemed_at": None,
            "title": "Full reset",
        }],
        "available_count": 1,
        "total_earned_count": 0,
    }


class _StubSession:
    """记录所有出站请求，供断言"发了几次、发到哪"。"""

    def __init__(self, get_payload=None, post_payload=None,
                 get_status=200, post_status=200):
        self.gets = []
        self.posts = []
        self._get = (get_status, get_payload if get_payload is not None else {})
        self._post = (post_status, post_payload if post_payload is not None else {})

    @staticmethod
    def _response(status, payload):
        response = Mock(status_code=status)
        response.json.return_value = payload
        response.text = "stub-body"
        return response

    def get(self, url, **kwargs):
        self.gets.append(url)
        return self._response(*self._get)

    def post(self, url, **kwargs):
        self.posts.append({"url": url, "json": kwargs.get("json")})
        return self._response(*self._post)


def _patch_db():
    return (
        patch.object(workspace_membership.db, "get_workspace_master",
                     return_value={"workspace_id": "workspace-1"}),
        patch.object(workspace_membership.db, "list_workspace_credentials_by_emails",
                     return_value=[{"email": "one@example.com",
                                    "access_token": "candidate-token",
                                    "quota_json": ""}]),
    )


class ListResetCreditsTests(unittest.TestCase):
    def _run(self, session):
        master, creds = _patch_db()
        with master, creds, patch.object(workspace_membership, "create_http_session",
                                         return_value=session):
            return workspace_membership.list_candidate_reset_credits(
                5, "one@example.com", proxy="socks5://pool-proxy:1080",
            )

    def test_parses_the_listing(self):
        session = _StubSession(get_payload=_listing_payload())
        result = self._run(session)
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(len(result["credits"]), 1)
        self.assertEqual(result["credits"][0]["id"], "RateLimitResetCredit_abc")
        self.assertTrue(session.gets[0].endswith("/backend-api/wham/rate-limit-reset-credits"))

    def test_listing_is_read_only(self):
        """只读接口绝不能发出 POST。"""
        session = _StubSession(get_payload=_listing_payload())
        self._run(session)
        self.assertEqual(session.posts, [])

    def test_requires_an_explicit_proxy(self):
        """候选人级请求禁止回退到母号出口或直连。"""
        with self.assertRaises(ValueError):
            workspace_membership.list_candidate_reset_credits(5, "one@example.com", proxy="")

    def test_401_maps_to_quota_unauthorized(self):
        """复用额度查询那套异常，上层的 401 重登分流才能直接接上。"""
        session = _StubSession(get_payload={}, get_status=401)
        with self.assertRaises(workspace_membership.QuotaUnauthorized):
            self._run(session)

    def test_5xx_maps_to_network_error(self):
        session = _StubSession(get_payload={}, get_status=503)
        with self.assertRaises(workspace_membership.QuotaNetworkError):
            self._run(session)


class ConsumeResetCreditTests(unittest.TestCase):
    def _run(self, session, credit_id=""):
        master, creds = _patch_db()
        with master, creds, patch.object(workspace_membership, "create_http_session",
                                         return_value=session):
            return workspace_membership.consume_candidate_reset_credit(
                5, "one@example.com", proxy="socks5://pool-proxy:1080", credit_id=credit_id,
            )

    def test_lists_before_consuming_and_posts_the_credit_id(self):
        session = _StubSession(
            get_payload=_listing_payload(),
            post_payload={"windows_reset": ["primary"], "code": "reset"},
        )
        result = self._run(session)
        # 必须先 GET 拿到 id 再 POST，不能盲发。
        self.assertEqual(len(session.gets), 1)
        self.assertEqual(len(session.posts), 1)
        self.assertTrue(session.posts[0]["url"].endswith(
            "/backend-api/wham/rate-limit-reset-credits/consume"))
        self.assertEqual(session.posts[0]["json"]["credit_id"], "RateLimitResetCredit_abc")
        self.assertEqual(result["windows_reset"], ["primary"])
        self.assertEqual(result["code"], "reset")

    def test_sends_a_fresh_idempotency_key_each_time(self):
        session = _StubSession(get_payload=_listing_payload(), post_payload={})
        first = self._run(session)
        second = self._run(session)
        for value in (first, second):
            self.assertTrue(value["redeem_request_id"])
        self.assertNotEqual(first["redeem_request_id"], second["redeem_request_id"])

    def test_refuses_when_no_credit_is_available(self):
        """没券时必须在本地就拦下来，不能白打一次 POST。"""
        session = _StubSession(get_payload={"credits": [], "available_count": 0})
        with self.assertRaises(workspace_membership.ResetCreditUnavailable):
            self._run(session)
        self.assertEqual(session.posts, [])

    def test_ignores_credits_that_are_not_available(self):
        session = _StubSession(get_payload=_listing_payload(status="redeemed"))
        with self.assertRaises(workspace_membership.ResetCreditUnavailable):
            self._run(session)
        self.assertEqual(session.posts, [])

    def test_unknown_credit_id_is_rejected_without_posting(self):
        session = _StubSession(get_payload=_listing_payload())
        with self.assertRaises(workspace_membership.ResetCreditUnavailable):
            self._run(session, credit_id="RateLimitResetCredit_not_mine")
        self.assertEqual(session.posts, [])

    def test_failed_consume_is_not_retried(self):
        """兑换失败绝不自动重试：重试可能烧掉第二张券。"""
        session = _StubSession(get_payload=_listing_payload(), post_status=500)
        with self.assertRaises(workspace_membership.QuotaNetworkError):
            self._run(session)
        self.assertEqual(len(session.posts), 1)

    def test_non_json_success_body_still_counts_as_consumed(self):
        """上游返回 200 但 body 不是 JSON 时，券已经扣了，不能当失败抛。"""
        session = _StubSession(get_payload=_listing_payload())
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("not json")
        session.post = lambda url, **kw: response
        result = self._run(session)
        self.assertTrue(result["credit_id"])


class ConsumeEndpointTests(unittest.TestCase):
    """API 层：额度重查失败不能污染兑换结果。"""

    def _req(self):
        return app.WorkspaceResetCreditReq(workspace_id=5, email="One@Example.com")

    def _patch_common(self):
        return (
            patch.object(app, "_reset_credit_target", return_value="one@example.com"),
            patch.object(app, "_lease_reset_credit_proxy", return_value="socks5://p:1080"),
            patch.object(app, "_workspace_settings_snapshot", return_value={}),
        )

    def test_quota_refresh_failure_does_not_report_the_consume_as_failed(self):
        target, proxy, settings = self._patch_common()
        with (
            target, proxy, settings,
            patch.object(app.workspace_membership, "consume_candidate_reset_credit",
                         return_value={"credit_id": "c1", "windows_reset": ["primary"]}),
            patch.object(app.workspace_membership, "fetch_candidate_quota",
                         side_effect=RuntimeError("boom")),
        ):
            result = app.api_consume_candidate_reset_credit(self._req())
        # 券已经扣掉了：ok 必须是 True，否则前端会提示失败、用户再点一次。
        self.assertTrue(result["ok"])
        self.assertEqual(result["consumed"]["credit_id"], "c1")
        self.assertIn("boom", result["quota_error"])

    def test_successful_flow_returns_the_refreshed_quota(self):
        target, proxy, settings = self._patch_common()
        with (
            target, proxy, settings,
            patch.object(app.workspace_membership, "consume_candidate_reset_credit",
                         return_value={"credit_id": "c1"}),
            patch.object(app.workspace_membership, "fetch_candidate_quota",
                         return_value={"primary": {"used_percent": 0}}),
        ):
            result = app.api_consume_candidate_reset_credit(self._req())
        self.assertTrue(result["ok"])
        self.assertEqual(result["quota"], {"primary": {"used_percent": 0}})
        self.assertEqual(result["quota_error"], "")

    def test_unavailable_credit_becomes_a_400(self):
        target, proxy, settings = self._patch_common()
        exc = workspace_membership.ResetCreditUnavailable("没有可兑换的重置券")
        with (
            target, proxy, settings,
            patch.object(app.workspace_membership, "consume_candidate_reset_credit",
                         side_effect=exc),
        ):
            with self.assertRaises(app.HTTPException) as ctx:
                app.api_consume_candidate_reset_credit(self._req())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_consume_failure_does_not_refresh_quota(self):
        """兑换没成功就别去重查额度，白打一次上游。"""
        target, proxy, settings = self._patch_common()
        with (
            target, proxy, settings,
            patch.object(app.workspace_membership, "consume_candidate_reset_credit",
                         side_effect=RuntimeError("upstream down")),
            patch.object(app.workspace_membership, "fetch_candidate_quota") as fetch,
        ):
            with self.assertRaises(app.HTTPException):
                app.api_consume_candidate_reset_credit(self._req())
        fetch.assert_not_called()


class ResetCreditTargetGuardTests(unittest.TestCase):
    """准入判定复用额度查询那套，已出库/已入箱/无凭证的账号不该动券。"""

    def _req(self, email="one@example.com"):
        return app.WorkspaceResetCreditReq(workspace_id=5, email=email)

    def test_rejects_an_empty_email(self):
        with self.assertRaises(app.HTTPException):
            app._reset_credit_target(app.WorkspaceResetCreditReq(workspace_id=5, email="  "))

    def test_rejects_a_candidate_outside_the_workspace(self):
        with patch.object(app.db, "get_workspace_candidate", return_value=None):
            with self.assertRaises(app.HTTPException) as ctx:
                app._reset_credit_target(self._req())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_an_ineligible_candidate(self):
        with (
            patch.object(app.db, "get_workspace_candidate", return_value={"email": "one@example.com"}),
            patch.object(app.db, "list_workspace_candidate_options",
                         return_value=[{"email": "one@example.com"}]),
            patch.object(app, "_candidate_quota_ineligible_reason", return_value="候选人已出库"),
        ):
            with self.assertRaises(app.HTTPException) as ctx:
                app._reset_credit_target(self._req())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("已出库", ctx.exception.detail)

    def test_outbound_and_trashed_rows_report_their_real_reason(self):
        """已出库/已入箱的行不在 options 里，但它们确实属于本空间。

        准入判定只看 options 会把这两类误报成"不属于当前空间"，掩盖真正的
        原因；候选人自身的字段必须盖在 options 之上。
        """
        for field, value, expected in (
            ("tag_status", "outbound", "已出库"),
            ("trash_status", "trashed", "垃圾箱"),
            ("trash_status", "scheduled", "排队入箱"),
        ):
            with self.subTest(field=field, value=value):
                row = {"email": "one@example.com", "workspace_join_status": "joined",
                       "has_workspace_access_token": True, "seat_type": "default",
                       field: value}
                with (
                    patch.object(app.db, "get_workspace_candidate", return_value=row),
                    # options 里没有这一行，正是线上的真实情况。
                    patch.object(app.db, "list_workspace_candidate_options", return_value=[]),
                ):
                    with self.assertRaises(app.HTTPException) as ctx:
                        app._reset_credit_target(self._req())
                self.assertIn(expected, ctx.exception.detail)
                self.assertNotIn("不属于当前空间", ctx.exception.detail)

    def test_normalizes_the_email(self):
        with (
            patch.object(app.db, "get_workspace_candidate", return_value={"email": "one@example.com"}),
            patch.object(app.db, "list_workspace_candidate_options",
                         return_value=[{"email": "one@example.com"}]),
            patch.object(app, "_candidate_quota_ineligible_reason", return_value=""),
        ):
            target = app._reset_credit_target(
                app.WorkspaceResetCreditReq(workspace_id=5, email="  One@Example.COM ")
            )
        self.assertEqual(target, "one@example.com")


if __name__ == "__main__":
    unittest.main()
