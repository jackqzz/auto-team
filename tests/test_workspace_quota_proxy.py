import unittest
from unittest.mock import Mock, call, patch

from fastapi import HTTPException

from webui import app, workspace_membership


class WorkspaceQuotaClientTests(unittest.TestCase):
    def test_candidate_quota_uses_explicit_pool_proxy_not_workspace_client(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "plan_type": "team",
            "rate_limit": {
                "allowed": True,
                "primary_window": {"used_percent": 10},
            },
            "credits": {},
        }
        session = Mock()
        session.get.return_value = response

        with (
            patch.object(
                workspace_membership.db,
                "get_workspace_master",
                return_value={"workspace_id": "workspace-1"},
            ),
            patch.object(
                workspace_membership.db,
                "list_workspace_credentials_by_emails",
                return_value=[{"email": "one@example.com", "access_token": "candidate-token"}],
            ),
            patch.object(workspace_membership.db, "update_workspace_quota"),
            patch.object(workspace_membership, "create_http_session", return_value=session) as create,
            patch.object(workspace_membership, "create_workspace_http_session") as workspace_client,
        ):
            result = workspace_membership.fetch_candidate_quota(
                5,
                "one@example.com",
                proxy="socks5://pool-proxy:1080",
            )

        self.assertTrue(result["allowed"])
        create.assert_called_once_with(proxy="socks5://pool-proxy:1080")
        workspace_client.assert_not_called()
        self.assertEqual(
            session.get.call_args.kwargs["headers"]["Authorization"],
            "Bearer candidate-token",
        )

    def test_candidate_quota_never_falls_back_when_pool_proxy_is_empty(self):
        with (
            patch.object(workspace_membership, "create_http_session") as create,
            patch.object(workspace_membership, "create_workspace_http_session") as workspace_client,
        ):
            with self.assertRaisesRegex(ValueError, "候选人代理池为空"):
                workspace_membership.fetch_candidate_quota(
                    5,
                    "one@example.com",
                    proxy="",
                )

        create.assert_not_called()
        workspace_client.assert_not_called()


class WorkspaceQuotaLeaseTests(unittest.TestCase):
    def test_workspace_settings_persist_prolite_seat_protection(self):
        request = app.WorkspaceQuotaScheduleReq(
            workspace_id=5,
            prolite_seat_protect_enabled=True,
            prolite_seat_protect_threshold=3,
            prolite_seat_protect_refresh_time="04:30",
        )
        with (
            patch.object(app.db, "get_workspace_settings", return_value={}) as get_settings,
            patch.object(app.db, "update_workspace_settings") as update_settings,
        ):
            app.api_save_workspace_candidate_settings(request)

        get_settings.assert_called_once_with(5)
        saved = update_settings.call_args.args[1]
        self.assertTrue(saved["prolite_seat_protect_enabled"])
        self.assertEqual(saved["prolite_seat_protect_threshold"], 3)
        self.assertEqual(saved["prolite_seat_protect_refresh_time"], "04:30")

    def test_one_quota_batch_leases_the_least_used_proxy(self):
        leases = app._candidate_quota_proxy_pool("proxy-one\nproxy-two")

        with patch.object(app.proxy_usage, "record_lease") as record:
            selected = [
                app._lease_candidate_quota_proxy(
                    leases,
                    workspace_id=5,
                    email=f"user-{index}@example.com",
                    detail="workspace_quota_scheduled",
                )
                for index in range(3)
            ]

        self.assertEqual(selected, ["proxy-one", "proxy-two", "proxy-one"])
        self.assertEqual(
            record.call_args_list,
            [
                call("proxy-one", "quota", "workspace_quota_scheduled"),
                call("proxy-two", "quota", "workspace_quota_scheduled"),
                call("proxy-one", "quota", "workspace_quota_scheduled"),
            ],
        )

    def test_manual_quota_honors_frontend_task_snapshot_selection(self):
        req = app.WorkspaceCandidatesReq(
            workspace_id=5,
            emails=["one@example.com"],
            proxy_pool="proxy-one\nproxy-two",
            quota_proxy="proxy-two",
        )
        rows = [{
            "email": "one@example.com",
            "has_workspace_access_token": True,
            "account_status": "active",
            "seat_type": "default",
        }]
        seen = []

        def fetch(_workspace_id, _email, *, proxy, **_kwargs):
            seen.append(proxy)
            return {"allowed": True, "primary": {"used_percent": 10}}

        with (
            patch.object(app, "_reject_trashed_candidates"),
            patch.object(app.db, "get_workspace_settings", return_value={"proxy_pool": req.proxy_pool}),
            patch.object(app.db, "list_workspace_candidate_options", return_value=rows),
            patch.object(app.db, "list_registered_invalid_emails", return_value=set()),
            patch.object(app.workspace_membership, "fetch_candidate_quota", side_effect=fetch),
            patch.object(app, "_clear_candidate_trash_timer"),
            patch.object(app.proxy_usage, "record_lease") as record,
        ):
            result = app.api_workspace_candidate_quota(req)

        self.assertTrue(result["results"]["one@example.com"]["ok"])
        self.assertEqual(seen, ["proxy-two"])
        record.assert_called_once_with(
            "proxy-two",
            "quota",
            "workspace_quota_manual",
        )

    def test_manual_quota_rejects_preselected_proxy_outside_current_pool(self):
        req = app.WorkspaceCandidatesReq(
            workspace_id=5,
            emails=["one@example.com"],
            proxy_pool="proxy-one",
            quota_proxy="proxy-two",
        )

        with (
            patch.object(app, "_reject_trashed_candidates"),
            patch.object(app.db, "get_workspace_settings", return_value={}),
        ):
            with self.assertRaises(HTTPException) as raised:
                app.api_workspace_candidate_quota(req)

        self.assertEqual(raised.exception.status_code, 400)

    def test_scheduled_batch_shares_one_balanced_proxy_snapshot(self):
        class StopAfterWait:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _seconds):
                self.stopped = True
                return True

        settings = {
            "proxy_pool": "proxy-one\nproxy-two",
            "concurrency": 2,
            "trash_enabled": False,
            "relogin_on_401": False,
        }
        rows = [
            {
                "email": "one@example.com",
                "has_workspace_access_token": True,
                "account_status": "active",
                "seat_type": "default",
            },
            {
                "email": "two@example.com",
                "has_workspace_access_token": True,
                "account_status": "active",
                "seat_type": "default",
            },
        ]
        seen = []

        def fetch(_workspace_id, _email, *, proxy, **_kwargs):
            seen.append(proxy)
            return {"allowed": True, "primary": {"used_percent": 10}}

        with (
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app.db, "list_workspace_candidate_options", return_value=rows),
            patch.object(app.workspace_membership, "fetch_candidate_quota", side_effect=fetch),
            patch.object(app, "_clear_candidate_trash_timer"),
            patch.object(app.proxy_usage, "record_lease") as record,
        ):
            app._quota_worker(
                5,
                30,
                StopAfterWait(),
                False,
                settings["proxy_pool"],
                False,
                2,
                180,
                1,
                0,
            )

        self.assertCountEqual(seen, ["proxy-one", "proxy-two"])
        self.assertEqual(record.call_count, 2)


class WorkspaceSeatProtectionWorkerTests(unittest.TestCase):
    def test_seat_switch_accepts_outbound_member(self):
        outbound = {
            "email": "outbound@example.com",
            "workspace_join_status": "joined",
            "member_id": "member-1",
            "seat_type": "default",
            "seat_label": "default",
            "tag_status": "outbound",
        }
        with (
            patch.object(
                app.db,
                "list_workspace_candidate_options",
                side_effect=lambda _wid, **kwargs: [outbound] if kwargs.get("tag_status") == "outbound" else [],
            ),
            patch.object(app.db, "get_workspace_settings", return_value={}),
            patch.object(app.workspace_membership, "update_member_seat_type", return_value={"success": True}) as update,
            patch.object(app.db, "update_workspace_candidate_member"),
        ):
            result = app.api_update_candidate_seat(
                app.WorkspaceCandidatesReq(
                    workspace_id=5,
                    emails=["outbound@example.com"],
                    seat_type="usage_based",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], 1)
        update.assert_called_once_with(5, "member-1", "usage_based")

    def test_exhausted_protection_skips_all_workspace_requests(self):
        class StopAfterWait:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _seconds):
                self.stopped = True
                return True

        settings = {
            "auto_standard_seat_enabled": True,
            "seat_protect_enabled": True,
            "seat_protect_used_count": 8,
            "seat_protect_threshold": 8,
        }
        stop = StopAfterWait()

        with (
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_refresh_workspace_seat_info") as refresh_seats,
            patch.object(app, "_refresh_workspace_unknown_candidate_seats") as refresh_members,
        ):
            app._auto_standard_seat_worker(987655, stop)

        refresh_seats.assert_not_called()
        refresh_members.assert_not_called()


class QuotaFailureTaxonomyTests(unittest.TestCase):
    """额度查询各类失败的分流行为。

    日志统计显示 83% 的定时查询失败是 TLS 握手失败/连接超时一类的网络错误，
    而 403 既打到过真正停用的账号，也打到过至今仍活跃的账号，所以这里的用例
    重点锁住两件事：网络类失败按配置重试，403 必须连续命中才判死。
    """

    def _patch_db(self, *, quota_json: str = "", update=None):
        """构造 fetch_candidate_quota 依赖的最小 db 桩。"""
        return (
            patch.object(
                workspace_membership.db,
                "get_workspace_master",
                return_value={"workspace_id": "workspace-1"},
            ),
            patch.object(
                workspace_membership.db,
                "list_workspace_credentials_by_emails",
                return_value=[{
                    "email": "one@example.com",
                    "access_token": "candidate-token",
                    "quota_json": quota_json,
                }],
            ),
            patch.object(
                workspace_membership.db,
                "update_workspace_quota",
                new=update or Mock(),
            ),
            patch.object(workspace_membership.db, "update_workspace_candidate_status"),
        )

    def _run(self, session, *, quota_json: str = "", network_retries: int = 2, update=None):
        master, creds, quota_update, status = self._patch_db(
            quota_json=quota_json, update=update
        )
        with (
            master, creds, quota_update, status,
            patch.object(workspace_membership, "create_http_session", return_value=session),
            patch.object(workspace_membership.time, "sleep"),
        ):
            return workspace_membership.fetch_candidate_quota(
                5,
                "one@example.com",
                proxy="socks5://pool-proxy:1080",
                network_retries=network_retries,
            )

    @staticmethod
    def _ok_response():
        response = Mock(status_code=200)
        response.json.return_value = {
            "plan_type": "team",
            "rate_limit": {"allowed": True, "primary_window": {"used_percent": 10}},
            "credits": {},
        }
        return response

    def test_network_error_retries_configured_times_then_raises(self):
        session = Mock()
        session.get.side_effect = OSError("TLS connect error")

        with self.assertRaises(workspace_membership.QuotaNetworkError):
            self._run(session, network_retries=2)

        # 首次 + 2 次重试
        self.assertEqual(session.get.call_count, 3)

    def test_network_retries_zero_does_not_retry(self):
        session = Mock()
        session.get.side_effect = OSError("TLS connect error")

        with self.assertRaises(workspace_membership.QuotaNetworkError):
            self._run(session, network_retries=0)

        self.assertEqual(session.get.call_count, 1)

    def test_network_error_recovers_within_budget(self):
        session = Mock()
        session.get.side_effect = [OSError("timed out"), self._ok_response()]

        result = self._run(session, network_retries=2)

        self.assertTrue(result["allowed"])
        self.assertEqual(session.get.call_count, 2)

    def test_5xx_shares_the_network_retry_budget(self):
        session = Mock()
        session.get.return_value = Mock(status_code=503, headers={})

        with self.assertRaises(workspace_membership.QuotaNetworkError):
            self._run(session, network_retries=2)

        self.assertEqual(session.get.call_count, 3)

    def test_5xx_recovers_within_budget(self):
        session = Mock()
        session.get.side_effect = [Mock(status_code=500, headers={}), self._ok_response()]

        result = self._run(session, network_retries=2)

        self.assertTrue(result["allowed"])

    def test_429_backs_off_then_succeeds(self):
        throttled = Mock(status_code=429, headers={"Retry-After": "2"})
        session = Mock()
        session.get.side_effect = [throttled, self._ok_response()]

        result = self._run(session)

        self.assertTrue(result["allowed"])
        self.assertEqual(session.get.call_count, 2)

    def test_429_exhausts_its_own_budget_not_the_network_one(self):
        session = Mock()
        session.get.return_value = Mock(status_code=429, headers={})

        with self.assertRaises(workspace_membership.QuotaHttpError) as ctx:
            self._run(session, network_retries=0)

        self.assertEqual(ctx.exception.status_code, 429)
        # 429 有独立预算，不受 network_retries=0 影响
        self.assertEqual(
            session.get.call_count,
            workspace_membership.WORKSPACE_ADMIN_MAX_429_RETRIES + 1,
        )

    def test_first_403_does_not_deactivate(self):
        session = Mock()
        session.get.return_value = Mock(status_code=403, headers={}, text="forbidden")
        update = Mock()

        with self.assertRaises(workspace_membership.QuotaHttpError) as ctx:
            self._run(session, quota_json="{}", update=update)

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertNotIsInstance(
            ctx.exception, workspace_membership.QuotaAccountDeactivated
        )
        self.assertEqual(update.call_args.args[2]["consecutive_403"], 1)

    def test_second_consecutive_403_deactivates(self):
        session = Mock()
        session.get.return_value = Mock(status_code=403, headers={}, text="forbidden")
        update = Mock()

        with self.assertRaises(workspace_membership.QuotaAccountDeactivated) as ctx:
            self._run(
                session,
                quota_json='{"consecutive_403": 1}',
                update=update,
            )

        self.assertEqual(ctx.exception.streak, 2)
        self.assertEqual(update.call_args.args[2]["consecutive_403"], 2)

    def test_success_clears_the_403_streak(self):
        session = Mock()
        session.get.return_value = self._ok_response()
        update = Mock()

        result = self._run(session, quota_json='{"consecutive_403": 1}', update=update)

        self.assertTrue(result["allowed"])
        # 成功落库的 payload 不含连击键，等同清零
        self.assertNotIn("consecutive_403", update.call_args.args[2])

    def test_402_is_payment_required_and_never_deactivates(self):
        session = Mock()
        session.get.return_value = Mock(status_code=402, headers={}, text="payment required")
        update = Mock()

        with self.assertRaises(workspace_membership.QuotaPaymentRequired):
            self._run(session, update=update)

        # 402 是空间级计费问题，绝不能带上账号级停用语义
        self.assertNotIn("consecutive_403", update.call_args.args[2])

    def test_401_still_raises_unauthorized(self):
        session = Mock()
        session.get.return_value = Mock(status_code=401, headers={}, text="")

        with self.assertRaises(workspace_membership.QuotaUnauthorized):
            self._run(session)


class CandidateProxyPoolResolutionTests(unittest.TestCase):
    """候选人任务代理池的优先级。

    前端各处仍会把全局池塞进请求体，所以空间专属池必须排在请求之前，否则
    配了也不生效；专属池为空时行为必须与改造前完全一致。
    """

    def test_dedicated_pool_wins_over_request_and_global(self):
        settings = {"quota_proxy_pool": "socks5://ded:1080", "proxy_pool": "socks5://glob:1080"}

        self.assertEqual(
            app._candidate_proxy_pool_text(settings, "socks5://req:1080"),
            "socks5://ded:1080",
        )

    def test_request_pool_used_when_no_dedicated_pool(self):
        settings = {"quota_proxy_pool": "", "proxy_pool": "socks5://glob:1080"}

        self.assertEqual(
            app._candidate_proxy_pool_text(settings, "socks5://req:1080"),
            "socks5://req:1080",
        )

    def test_falls_back_to_global_pool_when_unset(self):
        settings = {"proxy_pool": "socks5://glob:1080"}

        self.assertEqual(
            app._candidate_proxy_pool_text(settings, ""),
            "socks5://glob:1080",
        )

    def test_blank_dedicated_pool_is_not_treated_as_configured(self):
        settings = {"quota_proxy_pool": "   \n  ", "proxy_pool": "socks5://glob:1080"}

        self.assertEqual(
            app._candidate_proxy_pool_text(settings, ""),
            "socks5://glob:1080",
        )

    def test_scheduled_worker_uses_the_dedicated_pool(self):
        class StopAfterWait:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _seconds):
                self.stopped = True
                return True

        settings = {
            "quota_proxy_pool": "socks5://ded-one:1080\nsocks5://ded-two:1080",
            "proxy_pool": "socks5://glob:1080",
            "concurrency": 1,
            "relogin_on_401": False,
        }
        rows = [{"email": "one@example.com"}]
        seen = []

        def fetch(_workspace_id, _email, *, proxy, **_kwargs):
            seen.append(proxy)
            return {"allowed": True, "primary": {"used_percent": 10}}

        with (
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_candidate_quota_ineligible_reason", return_value=""),
            patch.object(app.db, "list_workspace_candidate_options", return_value=rows),
            patch.object(app.workspace_membership, "fetch_candidate_quota", side_effect=fetch),
            patch.object(app, "_clear_candidate_trash_timer"),
            patch.object(app.proxy_usage, "record_lease"),
        ):
            app._quota_worker(
                5, 30, StopAfterWait(), False, settings["proxy_pool"], False, 1, 180, 1, 0
            )

        self.assertEqual(len(seen), 1)
        self.assertIn(seen[0], ["socks5://ded-one:1080", "socks5://ded-two:1080"])


class CandidateProxyCooldownTests(unittest.TestCase):
    """失败代理熔断。

    日志显示 3 条代理制造了 70% 的失败（index 12/6/11 失败率约 50-59%），而
    偶发抖动的代理只有 0.3%。所以熔断必须按连击触发——一次就踢会把好代理
    误伤出池。
    """

    def _db(self, tmp):
        from pathlib import Path

        from webui import db

        return patch.object(db, "DB_PATH", Path(tmp) / "test.db")

    def test_cooldown_only_after_consecutive_threshold(self):
        import tempfile

        from webui import db

        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            db.init_db()
            proxy = "socks5://bad:1080"

            self.assertEqual(db.record_candidate_proxy_failure(proxy), 1)
            self.assertFalse(db.is_proxy_in_cooldown(proxy, db.CANDIDATE_PROXY_ERROR_TYPE))
            self.assertEqual(db.record_candidate_proxy_failure(proxy), 2)
            self.assertFalse(db.is_proxy_in_cooldown(proxy, db.CANDIDATE_PROXY_ERROR_TYPE))
            self.assertEqual(db.record_candidate_proxy_failure(proxy), 3)
            self.assertTrue(db.is_proxy_in_cooldown(proxy, db.CANDIDATE_PROXY_ERROR_TYPE))

    def test_success_clears_the_streak(self):
        import tempfile

        from webui import db

        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            db.init_db()
            proxy = "socks5://flaky:1080"

            db.record_candidate_proxy_failure(proxy)
            db.record_candidate_proxy_failure(proxy)
            db.clear_candidate_proxy_failure(proxy)

            # 清零后重新累计，两次失败仍不该熔断
            self.assertEqual(db.record_candidate_proxy_failure(proxy), 1)
            self.assertFalse(db.is_proxy_in_cooldown(proxy, db.CANDIDATE_PROXY_ERROR_TYPE))

    def test_cooldown_does_not_leak_into_other_error_types(self):
        import tempfile

        from webui import db

        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            db.init_db()
            proxy = "socks5://bad:1080"
            for _ in range(3):
                db.record_candidate_proxy_failure(proxy)

            # auto_loop 用的是自己的 error_type，不应被候选人熔断影响
            self.assertFalse(db.is_proxy_in_cooldown(proxy, "unable_to_load_site"))

    def test_lease_skips_cooled_down_proxy(self):
        import tempfile

        from webui import db, public_relogin

        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            db.init_db()
            bad = "socks5://bad:1080"
            for _ in range(3):
                db.record_candidate_proxy_failure(bad)

            pool = public_relogin.ProxyLeasePool([bad, "socks5://good:1080"])
            leased = [pool.lease(skip_cooldown=True)[0] for _ in range(4)]

            self.assertEqual(set(leased), {"socks5://good:1080"})

    def test_lease_without_flag_still_returns_cooled_proxy(self):
        """公开重登录等调用方共用本类，不应因候选人熔断而改变行为。"""
        import tempfile

        from webui import db, public_relogin

        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            db.init_db()
            bad = "socks5://bad:1080"
            for _ in range(3):
                db.record_candidate_proxy_failure(bad)

            pool = public_relogin.ProxyLeasePool([bad, "socks5://good:1080"])
            leased = [pool.lease()[0] for _ in range(2)]

            self.assertEqual(set(leased), {bad, "socks5://good:1080"})

    def test_all_cooled_down_pool_still_leases(self):
        """全部代理都在冷却时宁可用坏的，也不能让整批任务停摆。"""
        import tempfile

        from webui import db, public_relogin

        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            db.init_db()
            proxies = ["socks5://a:1080", "socks5://b:1080"]
            for proxy in proxies:
                for _ in range(3):
                    db.record_candidate_proxy_failure(proxy)

            pool = public_relogin.ProxyLeasePool(proxies)
            proxy, index, _ = pool.lease(skip_cooldown=True)

            self.assertIn(proxy, proxies)
            self.assertGreaterEqual(index, 0)

    def test_transport_failure_records_but_http_error_does_not(self):
        """只有传输层失败归因到代理；5xx 说明链路是通的，不该计入连击。"""
        import tempfile

        from webui import db

        with tempfile.TemporaryDirectory() as tmp, self._db(tmp):
            db.init_db()
            proxy = "socks5://pool-proxy:1080"
            helper = QuotaFailureTaxonomyTests()

            session = Mock()
            session.get.side_effect = OSError("TLS connect error")
            with self.assertRaises(workspace_membership.QuotaNetworkError):
                helper._run(session, network_retries=0)
            first = db.list_proxy_cooldown()
            self.assertEqual(
                [r["error_count"] for r in first if r["proxy"] == proxy], [1]
            )

            # 5xx 耗尽重试后同样抛 QuotaNetworkError，但连击计数被响应清零
            session = Mock()
            session.get.return_value = Mock(status_code=503, headers={})
            with self.assertRaises(workspace_membership.QuotaNetworkError):
                helper._run(session, network_retries=0)
            self.assertEqual(
                [r for r in db.list_proxy_cooldown() if r["proxy"] == proxy], []
            )


class SeatFulfillmentCounterTests(unittest.TestCase):
    """席位补齐历史累计计数器。

    抽屉里原先只显示 seat_fulfillment.*.count（当前状态快照），没有"一共补齐过
    多少席"的历史值；这里锁住累计语义：只增不减，且按席位类型分流。
    """

    def test_counter_accumulates_per_seat_type(self):
        import tempfile
        from pathlib import Path

        from webui import db

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                db.init_db()
                db.import_workspace_sessions(
                    '{"email": "m@example.com", "session_token": "%s"}' % ("s" * 64),
                    proxy="socks5://master-proxy:1080",
                )
                wid = db.list_workspace_masters()[0]["id"]

                self.assertEqual(db.increment_workspace_fulfillment_counter(wid, "prolite"), 1)
                self.assertEqual(db.increment_workspace_fulfillment_counter(wid, "prolite"), 2)
                self.assertEqual(db.increment_workspace_fulfillment_counter(wid, "default"), 1)

                settings = db.get_workspace_settings(wid)
                self.assertEqual(settings["prolite_fulfilled_total"], 2)
                self.assertEqual(settings["standard_fulfilled_total"], 1)

                stats = db.get_workspace_candidate_stats(wid)
                self.assertEqual(stats["seat_fulfillment"]["prolite"]["fulfilled_total"], 2)
                self.assertEqual(stats["seat_fulfillment"]["standard"]["fulfilled_total"], 1)


if __name__ == "__main__":
    unittest.main()
