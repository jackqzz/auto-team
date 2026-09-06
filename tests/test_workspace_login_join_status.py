"""仅登录空间任务跑完后，候选状态应当自动更新为"已加入"。

被邀请的成员一登录，上游就会自动接受邀请把它放进空间。这件事看账号自己的
空间列表就能确认，所以这里**不走**母号的候选人校验 —— 那个管理 API 限流很严，
而且成员刚接受完邀请时母号侧的 users/invites 还没同步出来，反过来会把已经
进空间的号写回 pending_invite。
"""
import json
import unittest
from unittest.mock import Mock, patch

from auth_flow import AuthFlow, AuthResult
from webui import registrar


MASTER_WORKSPACE = "11111111-2222-3333-4444-555555555555"
OTHER_WORKSPACE = "99999999-8888-7777-6666-555555555555"
EMAIL = "member@example.com"


def _cookie(payload: dict) -> str:
    import base64

    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"sig.{body}"


def _flow(*, cookie_payload=None, check_accounts=None, check_status=200, access_token="at"):
    """造一个只带本次断言所需依赖的 AuthFlow，不建真 HTTP 会话。"""
    flow = AuthFlow.__new__(AuthFlow)
    flow.result = AuthResult()
    flow.result.email = EMAIL
    flow.result.access_token = access_token
    flow._web_access_token = access_token
    flow._personal_only = False
    # 空间登录任务一定会带目标空间；它绝不能被当成"账号自己的空间"。
    flow._target_workspace_id = MASTER_WORKSPACE
    flow._http_trace_enabled = False
    flow._trace_dump_enabled = False
    flow._common_headers = Mock(return_value={})

    session = Mock()
    session.cookies.get.return_value = (
        _cookie(cookie_payload) if cookie_payload is not None else ""
    )
    response = Mock(status_code=check_status)
    response.json.return_value = {"accounts": check_accounts or {}}
    session.get.return_value = response
    flow.session = session
    return flow


def _options():
    return {"workspace_id": MASTER_WORKSPACE, "workspace_db_id": 7, "login_only": True}


class AccountWorkspaceListTests(unittest.TestCase):
    def test_target_workspace_override_is_not_mistaken_for_membership(self):
        """_extract_workspace_id 会直接回目标空间，据此判断等于永远为真。"""
        flow = _flow(cookie_payload={"workspaces": [{"id": OTHER_WORKSPACE}]},
                     check_accounts={})
        self.assertEqual(flow._extract_workspace_id(), MASTER_WORKSPACE)
        self.assertNotIn(MASTER_WORKSPACE, flow.list_account_workspace_ids())
        self.assertFalse(flow.account_is_in_workspace(MASTER_WORKSPACE))

    def test_reads_workspaces_from_auth_session_cookie(self):
        flow = _flow(
            cookie_payload={"workspaces": [{"id": OTHER_WORKSPACE}, {"id": MASTER_WORKSPACE}]},
            check_accounts={},
        )
        self.assertEqual(
            flow.list_account_workspace_ids(), [OTHER_WORKSPACE, MASTER_WORKSPACE]
        )

    def test_reads_workspaces_from_accounts_check(self):
        """cookie 里没有 workspaces 时，登录后的 accounts/check 才是权威列表。

        取值形状按 2026-09-04 实测响应：accounts 的 key 里既有空间 ID 也有
        "default"，后者重复指向个人空间，只能认 account.account_id。
        """
        flow = _flow(
            cookie_payload={},
            check_accounts={
                OTHER_WORKSPACE: {"account": {"account_id": OTHER_WORKSPACE,
                                              "structure": "personal"}},
                MASTER_WORKSPACE: {"account": {"account_id": MASTER_WORKSPACE,
                                               "structure": "workspace"}},
                "default": {"account": {"account_id": OTHER_WORKSPACE,
                                        "structure": "personal"}},
            },
        )
        self.assertEqual(
            flow.list_account_workspace_ids(), [OTHER_WORKSPACE, MASTER_WORKSPACE]
        )
        self.assertTrue(flow.account_is_in_workspace(MASTER_WORKSPACE))

    def test_accounts_check_failure_falls_back_to_cookie(self):
        flow = _flow(
            cookie_payload={"workspaces": [{"id": MASTER_WORKSPACE}]},
            check_status=429,
        )
        self.assertEqual(flow.list_account_workspace_ids(), [MASTER_WORKSPACE])

    def test_cookie_hit_does_not_spend_an_extra_request(self):
        """cookie 已经能证明在空间里，就没必要再打一次 accounts/check。"""
        flow = _flow(cookie_payload={"workspaces": [{"id": MASTER_WORKSPACE}]})
        self.assertTrue(flow.account_is_in_workspace(MASTER_WORKSPACE))
        flow.session.get.assert_not_called()

    def test_no_access_token_skips_the_http_call(self):
        flow = _flow(cookie_payload={"workspaces": [{"id": MASTER_WORKSPACE}]},
                     access_token="")
        self.assertEqual(flow.list_account_workspace_ids(), [MASTER_WORKSPACE])
        flow.session.get.assert_not_called()


class LoginJoinStatusSyncTests(unittest.TestCase):
    def _sync(self, workspace_ids, candidate, options=None):
        flow = Mock()
        flow.account_is_in_workspace.side_effect = lambda wid: wid in workspace_ids
        with (
            patch.object(registrar.db, "get_workspace_candidate", return_value=candidate),
            patch.object(
                registrar.db, "update_workspace_candidate_join_statuses"
            ) as update,
        ):
            written = registrar._sync_workspace_join_status(
                flow, _options() if options is None else options, EMAIL
            )
        return written, update

    def test_member_in_target_workspace_is_marked_joined(self):
        written, update = self._sync(
            [OTHER_WORKSPACE, MASTER_WORKSPACE],
            {"email": EMAIL, "workspace_join_status": "pending_invite"},
        )
        self.assertEqual(written, "joined")
        update.assert_called_once_with(7, [EMAIL], "joined")

    def test_member_not_in_target_workspace_keeps_its_status(self):
        """看不到目标空间的原因很多，不能反过来把 joined 降级。"""
        written, update = self._sync(
            [OTHER_WORKSPACE],
            {"email": EMAIL, "workspace_join_status": "joined"},
        )
        self.assertEqual(written, "")
        update.assert_not_called()

    def test_already_joined_is_not_rewritten(self):
        written, update = self._sync(
            [MASTER_WORKSPACE],
            {"email": EMAIL, "workspace_join_status": "joined"},
        )
        self.assertEqual(written, "")
        update.assert_not_called()

    def test_already_joined_costs_no_upstream_request(self):
        """本地已经是 joined 就别再打上游；批量仅登录时这是白花的请求。"""
        flow = Mock()
        with (
            patch.object(
                registrar.db, "get_workspace_candidate",
                return_value={"email": EMAIL, "workspace_join_status": "joined"},
            ),
            patch.object(registrar.db, "update_workspace_candidate_join_statuses"),
        ):
            registrar._sync_workspace_join_status(flow, _options(), EMAIL)
        flow.account_is_in_workspace.assert_not_called()

    def test_non_candidate_account_is_left_alone(self):
        written, update = self._sync([MASTER_WORKSPACE], None)
        self.assertEqual(written, "")
        update.assert_not_called()

    def test_plain_login_without_workspace_context_does_nothing(self):
        """注册结果页的普通重登录没有空间上下文，不该碰候选表。"""
        flow = Mock()
        with patch.object(registrar.db, "get_workspace_candidate") as get_candidate:
            written = registrar._sync_workspace_join_status(
                flow, {"login_only": True}, EMAIL
            )
        self.assertEqual(written, "")
        get_candidate.assert_not_called()
        flow.account_is_in_workspace.assert_not_called()

    def test_workspace_list_failure_does_not_break_the_login(self):
        flow = Mock()
        flow.account_is_in_workspace.side_effect = RuntimeError("429")
        with (
            patch.object(
                registrar.db, "get_workspace_candidate",
                return_value={"email": EMAIL, "workspace_join_status": "pending_invite"},
            ),
            patch.object(
                registrar.db, "update_workspace_candidate_join_statuses"
            ) as update,
        ):
            written = registrar._sync_workspace_join_status(flow, _options(), EMAIL)
        flow.account_is_in_workspace.assert_called_once_with(MASTER_WORKSPACE)
        self.assertEqual(written, "")
        update.assert_not_called()

    def test_membership_check_api_is_never_used(self):
        """明确约束：状态更新不得依赖母号的候选人校验。"""
        from webui import workspace_membership

        flow = Mock()
        flow.account_is_in_workspace.return_value = True
        with (
            patch.object(
                registrar.db, "get_workspace_candidate",
                return_value={"email": EMAIL, "workspace_join_status": "pending_invite"},
            ),
            patch.object(registrar.db, "update_workspace_candidate_join_statuses"),
            patch.object(
                workspace_membership, "check_candidate_membership"
            ) as check,
        ):
            registrar._sync_workspace_join_status(flow, _options(), EMAIL)
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
