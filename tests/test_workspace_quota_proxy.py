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
            with self.assertRaisesRegex(ValueError, "全局代理池为空"):
                workspace_membership.fetch_candidate_quota(
                    5,
                    "one@example.com",
                    proxy="",
                )

        create.assert_not_called()
        workspace_client.assert_not_called()


class WorkspaceQuotaLeaseTests(unittest.TestCase):
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

        def fetch(_workspace_id, _email, *, proxy):
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

        def fetch(_workspace_id, _email, *, proxy):
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


if __name__ == "__main__":
    unittest.main()
