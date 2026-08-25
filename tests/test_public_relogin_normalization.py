import unittest
import base64
import json
from unittest.mock import Mock, patch

from webui import public_relogin


class PublicReloginNormalizationTests(unittest.TestCase):
    @staticmethod
    def _token_with_account_id(account_id):
        payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"header.{encoded}.signature"

    def test_sub2api_camel_case_and_string_credentials(self):
        token = "opaque-access-token"
        account = {
            "credentials": '{"accessToken": "' + token + '", "email": "User@Example.com"}',
            "data": {"accountId": "workspace-1"},
        }
        normalized = public_relogin.normalized_account(account)
        self.assertEqual(normalized["access_token"], token)
        self.assertEqual(normalized["email"], "user@example.com")
        self.assertEqual(normalized["chatgpt_account_id"], "workspace-1")

    def test_jwt_account_id_wins_over_generic_export_id(self):
        account = {
            "credentials": {
                "access_token": self._token_with_account_id("jwt-workspace"),
                "account_id": "sub2-internal-id",
            },
        }
        normalized = public_relogin.normalized_account(account)
        self.assertEqual(normalized["chatgpt_account_id"], "jwt-workspace")

    @patch.object(public_relogin, "create_http_session")
    def test_quota_allows_personal_account_without_workspace_id(self, create_session):
        response = Mock(status_code=200)
        response.json.return_value = {
            "plan_type": "plus",
            "credits": {"balance": 12},
            "rate_limit": {},
        }
        session = Mock()
        session.get.return_value = response
        create_session.return_value = session

        result = public_relogin.fetch_quota(
            {"accessToken": "opaque-access-token", "email": "user@example.com"},
        )

        self.assertEqual(result["credits_balance"], 12)
        headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer opaque-access-token")
        self.assertNotIn("chatgpt-account-id", headers)
        self.assertNotIn("ChatGPT-Account-Id", headers)


if __name__ == "__main__":
    unittest.main()
