import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from webui import db, public_relogin

try:
    from webui import app
except ModuleNotFoundError as exc:  # pragma: no cover - 环境缺 fastapi 时跳过端点测试
    if exc.name != "fastapi":
        raise
    app = None


def _response(status_code, *, body="", headers=None, payload=None):
    response = Mock(status_code=status_code)
    response.text = body
    response.headers = headers or {}
    response.json.return_value = payload or {
        "plan_type": "team",
        "credits": {"balance": 7},
        "rate_limit": {"allowed": True},
    }
    return response


class _DbIsolatedTestCase(unittest.TestCase):
    """每个用例一份独立库——判死和 403 连击都是持久状态，共用库会串味。"""

    def setUp(self):
        self._old_db_path = db.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "webui.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._old_db_path
        self._tmp.cleanup()


class LooksDeactivatedTests(unittest.TestCase):
    def test_bare_403_in_text_is_not_a_deactivation_marker(self):
        # 曾经把裸 "403" 列为标志，导致 502 响应体和带 request_id 的错误
        # 都被判成账号停用（deactivated 在公开页是终态，等于永久踢出巡检）。
        self.assertFalse(public_relogin._looks_deactivated("upstream error id=44035"))
        self.assertFalse(public_relogin._looks_deactivated('{"error":"request_id 1403 failed"}'))
        self.assertFalse(public_relogin._looks_deactivated("HTTP 403 Forbidden"))

    def test_explicit_wording_still_matches(self):
        self.assertTrue(public_relogin._looks_deactivated("Your account has been deactivated"))
        self.assertTrue(public_relogin._looks_deactivated('{"detail":"user_deactivated"}'))


class FetchQuotaRateLimitTests(_DbIsolatedTestCase):
    def test_429_backs_off_then_succeeds(self):
        session = Mock()
        session.get.side_effect = [
            _response(429, headers={"Retry-After": "3"}),
            _response(200),
        ]
        with (
            patch.object(public_relogin, "create_http_session", return_value=session),
            patch.object(public_relogin.time, "sleep") as sleep,
        ):
            result = public_relogin.fetch_quota(
                {"access_token": "token", "email": "a@example.com"}, rate_retries=2,
            )
        self.assertEqual(result["credits_balance"], 7)
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_backoff_is_capped_at_15_seconds(self):
        # 上游给了个大得离谱的 Retry-After。这是同步 HTTP 端点，上层还会换代理
        # 重试 3 次，不能真睡这么久。
        session = Mock()
        session.get.side_effect = [
            _response(429, headers={"Retry-After": "600"}),
            _response(200),
        ]
        with (
            patch.object(public_relogin, "create_http_session", return_value=session),
            patch.object(public_relogin.time, "sleep") as sleep,
        ):
            public_relogin.fetch_quota({"access_token": "token"}, rate_retries=1)
        self.assertEqual(sleep.call_args.args[0], 15.0)

    def test_exhausted_backoff_raises_rate_limited(self):
        session = Mock()
        session.get.return_value = _response(429)
        with (
            patch.object(public_relogin, "create_http_session", return_value=session),
            patch.object(public_relogin.time, "sleep"),
        ):
            with self.assertRaises(public_relogin.PublicQuotaRateLimited):
                public_relogin.fetch_quota({"access_token": "token"}, rate_retries=2)
        self.assertEqual(session.get.call_count, 3)

    def test_zero_retries_means_no_backoff(self):
        session = Mock()
        session.get.return_value = _response(429)
        with (
            patch.object(public_relogin, "create_http_session", return_value=session),
            patch.object(public_relogin.time, "sleep") as sleep,
        ):
            with self.assertRaises(public_relogin.PublicQuotaRateLimited):
                public_relogin.fetch_quota({"access_token": "token"}, rate_retries=0)
        self.assertEqual(session.get.call_count, 1)
        sleep.assert_not_called()


class FetchQuotaForbiddenTests(_DbIsolatedTestCase):
    def _fetch(self, response):
        session = Mock()
        session.get.return_value = response
        with patch.object(public_relogin, "create_http_session", return_value=session):
            return public_relogin.fetch_quota(
                {"access_token": "token", "email": "a@example.com"},
                forbidden_streak=2,
            )

    def test_first_403_is_probation_not_death(self):
        with self.assertRaises(public_relogin.PublicQuotaForbidden):
            self._fetch(_response(403, body="forbidden"))

    def test_second_403_deactivates(self):
        with self.assertRaises(public_relogin.PublicQuotaForbidden):
            self._fetch(_response(403, body="forbidden"))
        with self.assertRaises(public_relogin.PublicAccountDeactivated):
            self._fetch(_response(403, body="forbidden"))

    def test_success_between_403s_clears_the_streak(self):
        with self.assertRaises(public_relogin.PublicQuotaForbidden):
            self._fetch(_response(403, body="forbidden"))
        self._fetch(_response(200))
        # 销案后重新从 1 开始数，所以这次仍是缓刑而不是判死。
        with self.assertRaises(public_relogin.PublicQuotaForbidden):
            self._fetch(_response(403, body="forbidden"))

    def test_explicit_deactivation_wording_skips_probation(self):
        with self.assertRaises(public_relogin.PublicAccountDeactivated) as ctx:
            self._fetch(_response(403, body='{"detail":"account deactivated"}'))
        self.assertNotIsInstance(ctx.exception, public_relogin.PublicWorkspaceDead)

    def test_account_without_email_is_never_deactivated_by_403(self):
        session = Mock()
        session.get.return_value = _response(403, body="forbidden")
        with patch.object(public_relogin, "create_http_session", return_value=session):
            for _ in range(5):
                with self.assertRaises(public_relogin.PublicQuotaForbidden):
                    public_relogin.fetch_quota({"access_token": "token"}, forbidden_streak=2)


class FetchQuota402Tests(_DbIsolatedTestCase):
    def _fetch_402(self, email, *, dead_threshold=3):
        session = Mock()
        session.get.return_value = _response(402, body="payment required")
        with patch.object(public_relogin, "create_http_session", return_value=session):
            public_relogin.fetch_quota(
                {"access_token": "token", "email": email, "workspace_id": "ws-1"},
                dead_threshold=dead_threshold,
            )

    def test_three_distinct_accounts_kill_the_workspace(self):
        for email in ("a@example.com", "b@example.com"):
            with self.assertRaises(public_relogin.PublicPaymentRequired):
                self._fetch_402(email)
        self.assertFalse(db.is_public_workspace_dead("ws-1"))

        with self.assertRaises(public_relogin.PublicWorkspaceDead):
            self._fetch_402("c@example.com")
        self.assertTrue(db.is_public_workspace_dead("ws-1"))

    def test_same_account_repeating_402_never_kills_the_workspace(self):
        # 用户要的是「3 个以上账号」，不是「3 次 402」。
        for _ in range(5):
            with self.assertRaises(public_relogin.PublicPaymentRequired):
                self._fetch_402("a@example.com")
        self.assertFalse(db.is_public_workspace_dead("ws-1"))

    def test_workspace_dead_is_a_deactivated_subclass(self):
        # 上层按 PublicAccountDeactivated 捕获的地方（免换代理的豁免元组）
        # 要能天然覆盖判死。
        self.assertTrue(
            issubclass(public_relogin.PublicWorkspaceDead, public_relogin.PublicAccountDeactivated)
        )


class DeadWorkspaceStoreTests(_DbIsolatedTestCase):
    def test_clear_also_wipes_402_details(self):
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            db.record_public_relogin_402("ws-1", email, dead_threshold=3)
        self.assertTrue(db.is_public_workspace_dead("ws-1"))

        self.assertTrue(db.clear_dead_public_workspace("ws-1"))
        self.assertFalse(db.is_public_workspace_dead("ws-1"))

        # 只删墓碑不删明细的话，解除后随便来一个 402 就会立刻重新达阈值。
        accounts, dead = db.record_public_relogin_402("ws-1", "a@example.com", dead_threshold=3)
        self.assertEqual(accounts, 1)
        self.assertFalse(dead)

    def test_clear_unknown_workspace_returns_false(self):
        self.assertFalse(db.clear_dead_public_workspace("nope"))

    def test_list_reports_account_count_and_note(self):
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            db.record_public_relogin_402("ws-1", email, dead_threshold=3)
        rows = db.list_dead_public_workspaces()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workspace_id"], "ws-1")
        self.assertEqual(rows[0]["account_count"], 3)
        self.assertIn("402", rows[0]["note"])

    def test_blank_identifiers_are_not_recorded(self):
        self.assertEqual(db.record_public_relogin_402("", "a@example.com"), (0, False))
        self.assertEqual(db.record_public_relogin_403(""), 0)
        self.assertEqual(db.list_dead_public_workspaces(), [])


@unittest.skipIf(app is None, "当前测试环境未安装 fastapi")
class PublicReloginDeadWorkspaceEndpointTests(_DbIsolatedTestCase):
    CFG = {
        "enabled": True,
        "concurrency": 1,
        "quota_timeout": 30,
        "login_timeout": 180,
        "retry_count": 0,
        "proxy_pool": "",
        "use_system_proxy_pool": True,
        "rate_limit_retries": 2,
        "forbidden_streak": 2,
        "payment_dead_accounts": 3,
    }

    def test_check_skips_accounts_of_a_dead_workspace(self):
        db.clear_dead_public_workspace("ws-1")
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            db.record_public_relogin_402("ws-1", email, dead_threshold=3)

        request = app.PublicReloginCheckReq(
            accounts=[{"id": "account-1", "email": "d@example.com", "workspace_id": "ws-1"}],
        )
        with (
            patch.object(public_relogin, "get_effective_config", return_value=self.CFG),
            patch.object(app, "_validate_public_relogin_access_key", return_value={}),
            patch.object(
                public_relogin, "fetch_quota",
                side_effect=AssertionError("判死空间不该再发额度请求"),
            ),
        ):
            response = asyncio.run(app.api_public_relogin_check(request))

        result = response["results"]["account-1"]
        self.assertEqual(result["status"], "deactivated")
        self.assertIn("已判定死亡", result["error"])

    def test_relogin_skips_accounts_of_a_dead_workspace(self):
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            db.record_public_relogin_402("ws-1", email, dead_threshold=3)

        with patch.object(public_relogin, "relogin_account") as relogin:
            result = app._run_public_relogin_account(
                {"id": "account-1"},
                {"email": "d@example.com", "chatgpt_account_id": "ws-1"},
                self.CFG,
                public_relogin.ProxyLeasePool([]),
            )
        relogin.assert_not_called()
        self.assertEqual(result["status"], "deactivated")

    def test_403_probation_reports_error_not_deactivated(self):
        request = app.PublicReloginCheckReq(
            accounts=[{"id": "account-1", "email": "a@example.com", "workspace_id": "ws-9"}],
        )
        with (
            patch.object(public_relogin, "get_effective_config", return_value=self.CFG),
            patch.object(app, "_validate_public_relogin_access_key", return_value={}),
            patch.object(
                public_relogin, "fetch_quota",
                side_effect=public_relogin.PublicQuotaForbidden("额度查询 403（缓刑 1/2）"),
            ) as fetch,
        ):
            response = asyncio.run(app.api_public_relogin_check(request))

        # 缓刑是非终态，前端下一轮还会再查这个账号。
        self.assertEqual(response["results"]["account-1"]["status"], "error")
        # 403 是账号级响应，换代理没有意义，所以只查了一次。
        self.assertEqual(fetch.call_count, 1)

    def test_rate_limited_still_rotates_proxies(self):
        request = app.PublicReloginCheckReq(
            accounts=[{"id": "account-1", "email": "a@example.com"}],
            proxy_pool="proxy-one\nproxy-two\nproxy-three",
        )
        with (
            patch.object(public_relogin, "get_effective_config", return_value=self.CFG),
            patch.object(app, "_validate_public_relogin_access_key", return_value={}),
            patch.object(
                public_relogin, "fetch_quota",
                side_effect=public_relogin.PublicQuotaRateLimited("持续限流"),
            ) as fetch,
            patch.object(public_relogin.proxy_usage, "record_lease"),
        ):
            response = asyncio.run(app.api_public_relogin_check(request))

        # 限流多半按出口 IP 算，换出口是正确的升级路径。
        self.assertEqual(fetch.call_count, 3)
        used = [call.kwargs["proxy"] for call in fetch.call_args_list]
        self.assertEqual(len(set(used)), 3)
        self.assertEqual(response["results"]["account-1"]["status"], "error")

    def test_first_402_death_short_circuits_the_rest_of_the_batch(self):
        db.record_public_relogin_402("ws-1", "a@example.com", dead_threshold=3)
        db.record_public_relogin_402("ws-1", "b@example.com", dead_threshold=3)
        self.assertFalse(db.is_public_workspace_dead("ws-1"))

        request = app.PublicReloginCheckReq(
            accounts=[
                {"id": "account-1", "email": "c@example.com", "workspace_id": "ws-1"},
                {"id": "account-2", "email": "d@example.com", "workspace_id": "ws-1"},
            ],
        )

        # concurrency=1 让派发器串行执行，队列先进先出，所以 account-1 一定先跑。
        # 它判死之后，account-2 必须在发请求之前就被本批的内存名单挡下来。
        calls = []

        def fake_fetch(account, **kwargs):
            calls.append(account.get("email"))
            if len(calls) > 1:
                raise AssertionError("判死后同空间的账号不该再发额度请求")
            raise public_relogin.PublicWorkspaceDead("workspace ws-1 判定空间死亡")

        with (
            patch.object(public_relogin, "get_effective_config", return_value=self.CFG),
            patch.object(app, "_validate_public_relogin_access_key", return_value={}),
            patch.object(public_relogin, "fetch_quota", side_effect=fake_fetch),
        ):
            response = asyncio.run(app.api_public_relogin_check(request))

        self.assertEqual(calls, ["c@example.com"])
        for key in ("account-1", "account-2"):
            self.assertEqual(response["results"][key]["status"], "deactivated")

    def test_dead_workspace_admin_endpoints(self):
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            db.record_public_relogin_402("ws-1", email, dead_threshold=3)

        listed = app.api_list_dead_public_workspaces()
        self.assertEqual([row["workspace_id"] for row in listed["workspaces"]], ["ws-1"])

        cleared = app.api_clear_dead_public_workspace("ws-1")
        self.assertEqual(cleared["workspaces"], [])

        with self.assertRaises(app.HTTPException) as ctx:
            app.api_clear_dead_public_workspace("ws-1")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
