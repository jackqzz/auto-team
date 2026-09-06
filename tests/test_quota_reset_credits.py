"""``wham/usage`` 里的额度重置券信息要落库，不能像以前那样整块丢掉。

上游每次查额度都会返回 ``rate_limit_reset_credits``（可用的额度重置券数）和
``rate_limit_reached_type``（额度耗尽的归因），历史解析器只保留 6 个字段，
把它们扔了。这里锁住解析行为，为后续"回收前先尝试重置额度"打底。

用例里的 payload 形状取自线上真实响应，不是臆造的。
"""
import unittest
from unittest.mock import Mock, patch

from webui import workspace_membership


class ResetCreditsParsingTests(unittest.TestCase):
    """``_parse_reset_credits`` / ``_reached_type`` 的纯解析行为。"""

    def test_parses_both_counts(self):
        payload = {"rate_limit_reset_credits": {
            "available_count": 1, "applicable_available_count": 0,
        }}
        self.assertEqual(
            workspace_membership._parse_reset_credits(payload),
            {"available": 1, "applicable": 0},
        )

    def test_missing_block_yields_empty_dict(self):
        """上游没返回这个块时不要凭空造出 0，调用方靠"键不存在"识别未知。"""
        self.assertEqual(workspace_membership._parse_reset_credits({}), {})
        self.assertEqual(
            workspace_membership._parse_reset_credits({"rate_limit_reset_credits": None}), {}
        )

    def test_missing_counts_become_none_not_zero(self):
        """0 是"确认没券"，None 是"上游没说"，两者不能混。"""
        parsed = workspace_membership._parse_reset_credits({"rate_limit_reset_credits": {}})
        self.assertEqual(parsed, {"available": None, "applicable": None})

    def test_non_numeric_counts_fall_back_to_none(self):
        payload = {"rate_limit_reset_credits": {
            "available_count": "abc", "applicable_available_count": True,
        }}
        self.assertEqual(
            workspace_membership._parse_reset_credits(payload),
            {"available": None, "applicable": None},
        )

    def test_reached_type_reads_the_nested_type_field(self):
        """线上返回的是 ``{"type": ..., "details": null}``，不是裸字符串。"""
        payload = {"rate_limit_reached_type": {
            "type": "workspace_member_credits_depleted", "details": None,
        }}
        self.assertEqual(
            workspace_membership._reached_type(payload), "workspace_member_credits_depleted"
        )

    def test_reached_type_also_accepts_a_bare_string(self):
        self.assertEqual(
            workspace_membership._reached_type({"rate_limit_reached_type": "usage_limit_reached"}),
            "usage_limit_reached",
        )

    def test_reached_type_is_empty_when_absent(self):
        self.assertEqual(workspace_membership._reached_type({}), "")
        self.assertEqual(workspace_membership._reached_type({"rate_limit_reached_type": None}), "")


class QuotaPayloadPersistenceTests(unittest.TestCase):
    """``fetch_candidate_quota`` 落库的记录要带上新解析出来的字段。"""

    def _run(self, payload):
        response = Mock(status_code=200)
        response.json.return_value = payload
        session = Mock()
        session.get.return_value = response
        update = Mock()
        with (
            patch.object(workspace_membership.db, "get_workspace_master",
                         return_value={"workspace_id": "workspace-1"}),
            patch.object(workspace_membership.db, "list_workspace_credentials_by_emails",
                         return_value=[{"email": "one@example.com",
                                        "access_token": "candidate-token",
                                        "quota_json": ""}]),
            patch.object(workspace_membership.db, "update_workspace_quota", new=update),
            patch.object(workspace_membership.db, "clear_candidate_proxy_failure"),
            patch.object(workspace_membership, "create_http_session", return_value=session),
        ):
            result = workspace_membership.fetch_candidate_quota(
                5, "one@example.com", proxy="socks5://pool-proxy:1080",
            )
        return result, update

    @staticmethod
    def _live_payload():
        """线上 prolite 账号的真实响应形状（有券但当前用不上）。"""
        return {
            "plan_type": "self_serve_business_prolite",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {"used_percent": 86, "limit_window_seconds": 604800,
                                   "reset_at": 1789187596},
                "secondary_window": None,
            },
            "credits": {"balance": None},
            "rate_limit_reached_type": None,
            "rate_limit_reset_credits": {"available_count": 1, "applicable_available_count": 0},
        }

    def test_reset_credits_are_persisted(self):
        result, update = self._run(self._live_payload())
        self.assertEqual(result["reset_credits"], {"available": 1, "applicable": 0})
        self.assertEqual(update.call_args[0][2]["reset_credits"], {"available": 1, "applicable": 0})

    def test_limit_reached_is_persisted(self):
        result, _ = self._run(self._live_payload())
        self.assertIs(result["limit_reached"], False)

    def test_reached_type_is_persisted_when_upstream_reports_one(self):
        """workspace 31 上真实出现过的形状：空间池子被掏空。"""
        payload = self._live_payload()
        payload["rate_limit_reached_type"] = {
            "type": "workspace_member_credits_depleted", "details": None,
        }
        result, _ = self._run(payload)
        self.assertEqual(result["rate_limit_reached_type"], "workspace_member_credits_depleted")

    def test_absent_fields_are_not_written_as_placeholders(self):
        """上游没返回的字段不写进记录，前端才能区分"未知"和"0"。"""
        result, _ = self._run({
            "plan_type": "team",
            "rate_limit": {"allowed": True, "primary_window": {"used_percent": 10}},
            "credits": {},
        })
        for key in ("reset_credits", "limit_reached", "rate_limit_reached_type"):
            self.assertNotIn(key, result)

    def test_existing_fields_are_untouched(self):
        """扩解析器不能动原有的 6 个字段，回收判定全靠它们。"""
        result, _ = self._run(self._live_payload())
        self.assertEqual(result["plan_type"], "self_serve_business_prolite")
        self.assertIsNone(result["credits_balance"])
        self.assertIs(result["allowed"], True)
        self.assertEqual(result["primary"], {"used_percent": 86, "window_seconds": 604800,
                                             "reset_at": 1789187596})
        self.assertEqual(result["secondary"], {"used_percent": None, "window_seconds": None,
                                               "reset_at": None})
        self.assertIn("updated_at", result)


class ZeroQuotaJudgementUnaffectedTests(unittest.TestCase):
    """新字段只是多存的信息，不能改变现有的额度耗尽判定。"""

    def test_reset_credits_do_not_make_an_exhausted_account_look_healthy(self):
        from webui import app
        payload = {
            "primary": {"used_percent": 100, "window_seconds": 604800},
            "secondary": {"used_percent": None, "window_seconds": None},
            "reset_credits": {"available": 1, "applicable": 1},
            "limit_reached": True,
            "rate_limit_reached_type": "workspace_member_credits_depleted",
        }
        self.assertTrue(app._is_zero_quota_payload(payload))

    def test_healthy_account_with_credits_is_still_healthy(self):
        from webui import app
        payload = {
            "primary": {"used_percent": 5, "window_seconds": 604800},
            "secondary": {"used_percent": None, "window_seconds": None},
            "reset_credits": {"available": 0, "applicable": 0},
        }
        self.assertFalse(app._is_zero_quota_payload(payload))


if __name__ == "__main__":
    unittest.main()
