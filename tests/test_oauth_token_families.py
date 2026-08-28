import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from auth_flow import AuthFlow, AuthResult, CODEX_OAUTH_CLIENT_ID


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class OAuthTokenFamilyTests(unittest.TestCase):
    @staticmethod
    def _jwt(payload):
        encode = lambda value: base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"

    def test_get_auth_session_does_not_overwrite_codex_access_token(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow.result.access_token = "codex-access"
        flow.result.id_token = "codex-id"
        flow.result.refresh_token = "codex-refresh"
        flow._codex_access_token = "codex-access"
        flow._env_overrides = {}
        flow._auth_session_fetched = False
        flow.session = SimpleNamespace(get=Mock(return_value=_Response({
            "accessToken": "web-access",
            "sessionToken": "web-session",
        })))
        flow._common_headers = Mock(return_value={})
        flow._trace_http = Mock()
        flow._extract_session_cookie = Mock(return_value="")
        flow._build_chatgpt_cookie_header = Mock(return_value="")

        session_token, returned_access = flow.get_auth_session()

        self.assertEqual(session_token, "web-session")
        self.assertEqual(returned_access, "web-access")
        self.assertEqual(flow.result.access_token, "codex-access")
        self.assertEqual(flow._web_access_token, "web-access")
        self.assertEqual(flow.result.id_token, "codex-id")
        self.assertEqual(flow.result.refresh_token, "codex-refresh")

    def test_get_auth_session_sets_web_access_before_codex_exchange(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow._codex_access_token = ""
        flow._env_overrides = {}
        flow._auth_session_fetched = False
        flow.session = SimpleNamespace(get=Mock(return_value=_Response({
            "accessToken": "web-access",
            "sessionToken": "web-session",
        })))
        flow._common_headers = Mock(return_value={})
        flow._trace_http = Mock()
        flow._extract_session_cookie = Mock(return_value="")
        flow._build_chatgpt_cookie_header = Mock(return_value="")

        flow.get_auth_session()

        self.assertEqual(flow.result.access_token, "web-access")
        self.assertEqual(flow._web_access_token, "web-access")

    def test_codex_exchange_token_survives_followup_web_session_refresh(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow._trace_http = Mock()
        flow._codex_exchange_error_code = ""
        flow._captured_login_verifier = ""
        flow._ua = "test-agent"
        access = self._jwt({"client_id": CODEX_OAUTH_CLIENT_ID})
        identity = self._jwt({"at_hash": "placeholder"})
        response = _Response({
            "access_token": access,
            "id_token": identity,
            "refresh_token": "codex-refresh",
        })
        flow.session = SimpleNamespace(post=Mock(return_value=response))

        self.assertTrue(flow._exchange_codex_callback_code(
            "http://localhost:1455/auth/callback?code=abc&state=state",
            "state",
            "verifier",
            "http://localhost:1455/auth/callback",
            CODEX_OAUTH_CLIENT_ID,
        ))
        self.assertEqual(flow.result.access_token, access)
        self.assertEqual(flow._codex_access_token, access)
        self.assertEqual(flow.result.id_token, identity)
        self.assertEqual(flow.result.refresh_token, "codex-refresh")

        flow._env_overrides = {}
        flow._auth_session_fetched = False
        flow.session.get = Mock(return_value=_Response({
            "accessToken": "web-access",
            "sessionToken": "web-session",
        }))
        flow._common_headers = Mock(return_value={})
        flow._extract_session_cookie = Mock(return_value="")
        flow._build_chatgpt_cookie_header = Mock(return_value="")
        flow.get_auth_session()
        self.assertEqual(flow.result.access_token, access)
        self.assertEqual(flow._web_access_token, "web-access")


if __name__ == "__main__":
    unittest.main()
