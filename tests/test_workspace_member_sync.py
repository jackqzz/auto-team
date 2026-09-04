import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

try:
    from fastapi import HTTPException
    from webui import app, workspace_membership
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    HTTPException = None
    app = None
    workspace_membership = None


@unittest.skipIf(app is None, "当前测试环境未安装 fastapi")
class WorkspaceMemberSyncApiTests(unittest.TestCase):
    def test_invite_persists_pending_invite_seat_type(self):
        request = app.WorkspaceCandidatesReq(
            workspace_id=5,
            emails=["member@example.com"],
            seat_type="usage_based",
        )
        states = {"member@example.com": "pending_invite"}
        seats = {
            "member@example.com": {
                "raw_seat_type": "usage_based",
                "codex_seat": "Codex席位",
                "gpt_seat": "",
                "member_id": "",
            },
        }
        with (
            patch.object(app, "_reject_permanently_invalid"),
            patch.object(app, "_reject_trashed_candidates"),
            patch.object(app.db, "list_workspace_candidates", return_value=[{"email": "member@example.com"}]),
            patch.object(app.workspace_membership, "invite_candidates", return_value={"ok": True}),
            patch.object(app.workspace_membership, "check_candidate_membership", return_value=(states, seats)),
            patch.object(app.db, "update_workspace_candidate_status") as update_status,
            patch.object(app.db, "update_workspace_candidate_seat_type") as update_seat,
            patch.object(app, "_INVITE_STATUS_RECHECK_DELAY_SECONDS", 0),
        ):
            result = app.api_invite_workspace_candidates(request)

        self.assertEqual(result["states"], states)
        update_status.assert_called_once_with(5, "member@example.com", "pending_invite")
        update_seat.assert_called_once_with(5, "member@example.com", "usage_based")

    def test_seat_statistics_sync_does_not_refresh_members(self):
        seat_info = {
            "seats_in_use": 2,
            "seats_entitled": 3,
            "seats_default": 2,
            "seats_usage_based": 0,
        }
        with (
            patch.object(app.workspace_membership, "sync_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info") as update,
            patch.object(app, "_refresh_workspace_unknown_candidate_seats") as refresh,
        ):
            result = app.api_sync_workspace(5)

        self.assertTrue(result["ok"])
        update.assert_called_once()
        refresh.assert_not_called()

    def test_member_sync_has_an_independent_endpoint(self):
        expected = {"requested": 20, "refreshed": 18, "missing": 2, "remaining": 4}
        with patch.object(
            app,
            "_refresh_workspace_unknown_candidate_seats",
            return_value=expected,
        ) as refresh:
            result = app.api_sync_workspace_members(5)

        self.assertEqual(result, {"ok": True, **expected})
        refresh.assert_called_once_with(5)

    def test_member_sync_rejects_duplicate_workspace_job(self):
        with app._workspace_member_sync_lock:
            app._workspace_member_sync_running.add(5)
        try:
            with self.assertRaises(HTTPException) as raised:
                app.api_sync_workspace_members(5)
        finally:
            with app._workspace_member_sync_lock:
                app._workspace_member_sync_running.discard(5)
        self.assertEqual(raised.exception.status_code, 409)


@unittest.skipIf(workspace_membership is None, "当前测试环境未安装 fastapi")
class WorkspaceMemberPaginationTests(unittest.TestCase):
    def setUp(self):
        with workspace_membership._workspace_admin_state_lock:
            workspace_membership._workspace_admin_last_completed.clear()
            workspace_membership._workspace_admin_cooldown_until.clear()

    def test_subscription_capacity_keeps_default_and_prolite_purchased_counts(self):
        def response(payload):
            item = Mock(status_code=200, headers={})
            item.json.return_value = payload
            return item

        session = Mock()
        master = {"workspace_id": "workspace-1", "access_token": "token"}
        responses = [
            response({
                "seats_in_use": 76,
                "seats_entitled": 8,
                "seat_capacity": [
                    {"type": "default", "paid": 4, "available": 4},
                    {"type": "prolite", "paid": 4, "available": 1, "held": 2},
                ],
            }),
            response({"seat_type_counts": {"default": 0, "usage_based": 73, "prolite": 3}}),
            response({"amount_due": {"amount": 0, "currency": "sgd"}, "renewal_date": "2026-09-14"}),
        ]
        with (
            patch.object(workspace_membership, "create_workspace_http_session", return_value=(session, master)),
            patch.object(workspace_membership, "_workspace_admin_request", side_effect=responses),
        ):
            result = workspace_membership.sync_seat_info(5)

        self.assertEqual(result["seats_entitled"], 8)
        self.assertEqual(result["seats_default_entitled"], 4)
        self.assertEqual(result["seats_prolite_entitled"], 4)
        self.assertEqual(result["seats_default"], 0)
        self.assertEqual(result["seats_prolite"], 3)
        self.assertEqual(result["seats_usage_based"], 73)
        self.assertEqual(result["seats_default_available"], 4)
        self.assertEqual(result["seats_prolite_available"], 1)
        # 缺 held 字段按 0 处理，有则原样取；prolite 这行满足 paid = 在用 + held + available。
        self.assertEqual(result["seats_default_held"], 0)
        self.assertEqual(result["seats_prolite_held"], 2)

    @staticmethod
    def _mock_response(payload):
        item = Mock(status_code=200, headers={})
        item.json.return_value = payload
        return item

    def _sync_with(self, subscriptions, preview):
        session = Mock()
        master = {"workspace_id": "workspace-1", "access_token": "token"}
        responses = [
            self._mock_response(subscriptions),
            self._mock_response({"seat_type_counts": {"default": 4, "usage_based": 71, "prolite": 4}}),
            self._mock_response(preview),
        ]
        with (
            patch.object(workspace_membership, "create_workspace_http_session", return_value=(session, master)),
            patch.object(workspace_membership, "_workspace_admin_request", side_effect=responses),
        ):
            return workspace_membership.sync_seat_info(5)

    def test_seat_cost_falls_back_to_subscription_billing_currency(self):
        # preview 不返回 currency 时（线上确实出现过，库里存成 "58.36 "），
        # 用 subscriptions 的 billing_currency 兜底。
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8, "billing_currency": "SGD"},
            {"amount_due": {"amount": 5836}, "renewal_date": "2026-09-14"},
        )
        self.assertEqual(result["seat_cost"], "58.36 SGD")

    def test_seat_cost_prefers_preview_currency_over_subscription(self):
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8, "billing_currency": "SGD"},
            {"amount_due": {"amount": 5836, "currency": "usd"}},
        )
        self.assertEqual(result["seat_cost"], "58.36 USD")

    def test_seat_cost_reads_currency_from_preview_top_level(self):
        # 实测线上 amount_due 里没有 currency，币种在 preview 顶层。
        # 只读 amount_due.currency 正是库里 "58.36 " 缺币种的成因。
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8},
            {"amount_due": {"amount": 5821}, "currency": "sgd"},
        )
        self.assertEqual(result["seat_cost"], "58.21 SGD")

    def test_seat_cost_without_any_currency_has_no_trailing_space(self):
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8},
            {"amount_due": {"amount": 5836}},
        )
        self.assertEqual(result["seat_cost"], "58.36")

    def test_subscription_flags_are_normalized_to_int(self):
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8, "will_renew": True, "is_delinquent": False},
            {"amount_due": {"amount": 0}},
        )
        self.assertEqual(result["will_renew"], 1)
        self.assertEqual(result["is_delinquent"], 0)

        delinquent = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8, "will_renew": False, "is_delinquent": True},
            {"amount_due": {"amount": 0}},
        )
        self.assertEqual(delinquent["will_renew"], 0)
        self.assertEqual(delinquent["is_delinquent"], 1)

    def test_missing_will_renew_defaults_to_renewing(self):
        # 响应里没这个字段时不能当成 False，否则页面会凭空显示"（不再续订）"。
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8},
            {"amount_due": {"amount": 0}},
        )
        self.assertEqual(result["will_renew"], 1)
        self.assertEqual(result["is_delinquent"], 0)

    def test_renewal_date_falls_back_to_subscription_active_until(self):
        # preview 会返回同一个日期（两个空间实测一致），但它是第三个请求。
        # preview 没给日期时用 subscriptions 的 active_until 兜底。
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8, "active_until": "2026-09-14T14:15:25Z"},
            {"amount_due": {"amount": 0}},
        )
        self.assertEqual(result["renewal_date"], "2026-09-14T14:15:25Z")

    def test_renewal_date_prefers_preview_over_active_until(self):
        # 有 pending 变更时两者可能不同，preview 是加购后的实际账期，优先它。
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8, "active_until": "2026-09-14T14:15:25Z"},
            {"amount_due": {"amount": 0}, "renewal_date": "2026-10-01T00:00:00Z"},
        )
        self.assertEqual(result["renewal_date"], "2026-10-01T00:00:00Z")

    def test_preview_failure_keeps_seat_counts_and_active_until(self):
        # preview 只贡献 seat_cost，它挂了不该把前两个请求已拿到的席位数一起丢掉。
        session = Mock()
        master = {"workspace_id": "workspace-1", "access_token": "token"}
        responses = [
            self._mock_response({
                "seats_in_use": 79,
                "seats_entitled": 8,
                "active_until": "2026-09-14T14:15:25Z",
                "seat_capacity": [{"type": "default", "paid": 4, "available": 0, "held": 1}],
            }),
            self._mock_response({"seat_type_counts": {"default": 3, "usage_based": 71, "prolite": 4}}),
            workspace_membership.UpstreamHttpError(500, "boom"),
        ]
        with (
            patch.object(workspace_membership, "create_workspace_http_session", return_value=(session, master)),
            patch.object(workspace_membership, "_workspace_admin_request", side_effect=responses),
        ):
            result = workspace_membership.sync_seat_info(5)

        self.assertEqual(result["seats_default"], 3)
        self.assertEqual(result["seats_default_entitled"], 4)
        self.assertEqual(result["seats_default_held"], 1)
        self.assertEqual(result["seats_default_available"], 0)
        self.assertEqual(result["renewal_date"], "2026-09-14T14:15:25Z")
        self.assertEqual(result["seat_cost"], "")

    def test_missing_seat_capacity_leaves_available_unknown(self):
        # 没有 seat_capacity 时 available 必须留 None（"未同步"），
        # 落成 0 会让前端误报"无空位"。held 同理，不能凭空显示"待处理 0 席"。
        result = self._sync_with(
            {"seats_in_use": 79, "seats_entitled": 8},
            {"amount_due": {"amount": 0}},
        )
        self.assertIsNone(result["seats_default_available"])
        self.assertIsNone(result["seats_prolite_available"])
        self.assertIsNone(result["seats_default_held"])
        self.assertIsNone(result["seats_prolite_held"])

    def test_bulk_member_sync_paginates_and_matches_locally(self):
        first = Mock(status_code=200, headers={})
        first.json.return_value = {
            "items": [
                {"email": "one@example.com", "id": "member-1", "seat_type": "default"},
                {"email": "unused@example.com", "id": "member-x", "seat_type": "default"},
            ],
            "total": 3,
        }
        second = Mock(status_code=200, headers={})
        second.json.return_value = {
            "items": [
                {"email": "two@example.com", "id": "member-2", "seat_type": "usage_based"},
            ],
            "total": 3,
        }
        session = Mock()
        session.get.side_effect = [first, second]
        master = {"workspace_id": "workspace-1", "access_token": "token"}

        with patch.object(
            workspace_membership,
            "create_workspace_http_session",
            return_value=(session, master),
        ):
            result = workspace_membership.fetch_candidate_seats_bulk(
                5,
                ["one@example.com", "two@example.com"],
                page_size=2,
                request_interval=0,
            )

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(result["one@example.com"]["raw_seat_type"], "default")
        self.assertEqual(result["two@example.com"]["raw_seat_type"], "usage_based")

    def test_bulk_member_sync_retries_429(self):
        limited = Mock(status_code=429, headers={"Retry-After": "1"})
        limited.json.return_value = {"detail": "Too many requests"}
        success = Mock(status_code=200, headers={})
        success.json.return_value = {
            "items": [
                {"email": "one@example.com", "id": "member-1", "seat_type": "default"},
            ],
            "total": 1,
        }
        session = Mock()
        session.get.side_effect = [limited, success]
        master = {"workspace_id": "workspace-1", "access_token": "token"}

        with (
            patch.object(
                workspace_membership,
                "create_workspace_http_session",
                return_value=(session, master),
            ),
            patch.object(workspace_membership.time, "sleep") as sleep,
        ):
            result = workspace_membership.fetch_candidate_seats_bulk(
                5,
                ["one@example.com"],
                request_interval=0,
            )

        self.assertIn("one@example.com", result)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 1.0, delta=0.1)


@unittest.skipIf(workspace_membership is None, "当前测试环境未安装 fastapi")
class WorkspaceMembershipThrottleTests(unittest.TestCase):
    def setUp(self):
        with workspace_membership._workspace_admin_state_lock:
            workspace_membership._workspace_admin_last_completed.clear()
            workspace_membership._workspace_admin_cooldown_until.clear()

    def test_post_invite_check_uses_one_invite_list_request_when_all_match(self):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {
            "items": [
                {"email": "one@example.com", "status": 2, "seat_type": "default"},
                {"email": "two@example.com", "status": 2, "seatType": "usage_based"},
            ],
        }
        session = Mock()
        session.get.return_value = response
        master = {"workspace_id": "workspace-1", "access_token": "token"}

        with (
            patch.object(
                workspace_membership,
                "create_workspace_http_session",
                return_value=(session, master),
            ),
            patch.object(workspace_membership, "WORKSPACE_ADMIN_REQUEST_INTERVAL_SECONDS", 0),
        ):
            states, seats = workspace_membership.check_candidate_membership(
                11,
                ["one@example.com", "two@example.com"],
                prefer_invites=True,
                include_seats=True,
            )

        self.assertEqual(
            states,
            {
                "one@example.com": "pending_invite",
                "two@example.com": "pending_invite",
            },
        )
        self.assertEqual(seats["one@example.com"]["raw_seat_type"], "default")
        self.assertEqual(seats["one@example.com"]["gpt_seat"], "GPT席位")
        self.assertEqual(seats["two@example.com"]["raw_seat_type"], "usage_based")
        self.assertEqual(seats["two@example.com"]["codex_seat"], "Codex席位")
        session.get.assert_called_once()
        self.assertTrue(session.get.call_args.args[0].endswith("/invites"))

    def test_membership_check_returns_seats_from_the_same_member_response(self):
        member_response = Mock(status_code=200, headers={})
        member_response.json.return_value = {
            "items": [
                {"email": "one@example.com", "id": "member-1", "seat_type": "default"},
            ],
        }
        session = Mock()
        session.get.return_value = member_response
        master = {"workspace_id": "workspace-1", "access_token": "token"}

        with (
            patch.object(
                workspace_membership,
                "create_workspace_http_session",
                return_value=(session, master),
            ),
            patch.object(workspace_membership, "WORKSPACE_ADMIN_REQUEST_INTERVAL_SECONDS", 0),
        ):
            states, seats = workspace_membership.check_candidate_membership(
                12,
                ["one@example.com"],
                include_seats=True,
            )

        self.assertEqual(states["one@example.com"], "joined")
        self.assertEqual(seats["one@example.com"]["member_id"], "member-1")
        self.assertEqual(seats["one@example.com"]["raw_seat_type"], "default")
        session.get.assert_called_once()

    def test_exhausted_429_does_not_report_candidate_as_not_invited(self):
        limited = Mock(status_code=429, headers={"Retry-After": "1"})
        limited.json.return_value = {"detail": "Too many requests"}
        session = Mock()
        session.get.return_value = limited
        master = {"workspace_id": "workspace-1", "access_token": "token"}

        with (
            patch.object(
                workspace_membership,
                "create_workspace_http_session",
                return_value=(session, master),
            ),
            patch.object(workspace_membership.time, "sleep"),
        ):
            with self.assertRaises(workspace_membership.UpstreamHttpError) as raised:
                workspace_membership.check_candidate_membership(
                    15,
                    ["one@example.com"],
                    prefer_invites=True,
                )

        self.assertEqual(raised.exception.status_code, 429)

    def test_same_workspace_admin_requests_never_overlap(self):
        state_lock = threading.Lock()
        active = 0
        max_active = 0
        response = Mock(status_code=200, headers={})

        class Session:
            def get(self, _url, **_kwargs):
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                with state_lock:
                    active -= 1
                return response

        session = Session()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    workspace_membership._workspace_admin_request,
                    13,
                    session,
                    "get",
                    "https://example.test/users",
                    request_interval=0,
                )
                for _ in range(2)
            ]
            for future in futures:
                self.assertIs(future.result(), response)

        self.assertEqual(max_active, 1)

    def test_admin_request_retries_once_after_refreshing_expired_access_token(self):
        expired = Mock(status_code=401, headers={})
        expired.json.return_value = {"detail": {"code": "token_expired"}}
        fresh = Mock(status_code=200, headers={})
        session = Mock()
        session.get.side_effect = [expired, fresh]
        with (
            patch.object(workspace_membership, "_refresh_workspace_access_token", return_value="fresh-token") as refresh,
            patch.object(workspace_membership, "_workspace_external_id", return_value="workspace-1"),
        ):
            result = workspace_membership._workspace_admin_request(
                16,
                session,
                "get",
                "https://example.test/users",
                headers={"Authorization": "Bearer expired"},
                request_interval=0,
            )

        self.assertIs(result, fresh)
        refresh.assert_called_once_with(16, session)
        self.assertEqual(session.get.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer fresh-token")

    def test_admin_headers_include_browser_device_id(self):
        headers = workspace_membership._headers("token", "workspace-1")
        self.assertTrue(headers["oai-device-id"])
        self.assertEqual(headers["Referer"], "https://chatgpt.com/admin/members")

    def test_same_workspace_waits_after_previous_response(self):
        response = Mock(status_code=200, headers={})
        session = Mock()
        session.get.return_value = response

        with (
            patch.object(
                workspace_membership.time,
                "monotonic",
                side_effect=[100.0, 100.1, 100.1, 101.1],
            ),
            patch.object(workspace_membership.time, "sleep") as sleep,
        ):
            workspace_membership._workspace_admin_request(
                14,
                session,
                "get",
                "https://example.test/users",
                request_interval=1.0,
            )
            workspace_membership._workspace_admin_request(
                14,
                session,
                "get",
                "https://example.test/users",
                request_interval=1.0,
            )

        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
