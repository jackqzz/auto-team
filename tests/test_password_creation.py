import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from auth_flow import AuthFlow, AuthResult


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _flow(response):
    flow = AuthFlow.__new__(AuthFlow)
    flow.result = AuthResult()
    flow.result.device_id = ""
    flow.session = SimpleNamespace(
        get=Mock(return_value=_Response(200)),
        post=Mock(return_value=response),
    )
    flow._common_headers = Mock(return_value={})
    flow._trace_http = Mock()
    flow._on_password = Mock()
    flow._random_password = Mock(return_value="Generated!12345")
    flow._last_sentinel_token = ""
    flow._last_sentinel_so_token = ""
    return flow


class PasswordCreationTests(unittest.TestCase):
    def test_password_creation_reuses_history_before_generating_new_candidate(self):
        flow = _flow(_Response(409, {"error": "temporary upstream error"}))
        flow._account_callback = Mock(return_value={"password": "Historical!123"})

        ok = flow.register_password("passwordless@example.com")

        self.assertFalse(ok)
        self.assertEqual(flow.result.password, "Historical!123")
        self.assertEqual(
            flow.session.post.call_args.kwargs["json"]["password"],
            "Historical!123",
        )
        flow._random_password.assert_not_called()

    def test_existing_passwordless_uses_user_register_api(self):
        flow = _flow(_Response(200, {"continue_url": "/email-verification"}))

        ok = flow.create_password_via_api(
            "passwordless@example.com",
            "Created!Password123",
        )

        self.assertTrue(ok)
        self.assertEqual(flow.result.password, "Created!Password123")
        flow.session.post.assert_called_once()
        # Existing-account creation establishes the same registration password
        # step as a normal signup.  It must never navigate to reset-password.
        flow.session.get.assert_called_once()
        self.assertEqual(
            flow.session.get.call_args.args[0],
            "https://auth.openai.com/create-account/password",
        )
        request = flow.session.post.call_args
        self.assertEqual(
            request.args[0],
            "https://auth.openai.com/api/accounts/user/register",
        )
        self.assertEqual(request.kwargs["json"], {
            "password": "Created!Password123",
            "username": "passwordless@example.com",
        })
        flow._on_password.assert_called_once_with(
            "passwordless@example.com",
            "Created!Password123",
        )

    def test_failed_creation_keeps_password_candidate_for_retry(self):
        flow = _flow(_Response(409, {"error": "already has password"}, "already has password"))

        ok = flow.create_password_via_api(
            "passwordless@example.com",
            "Created!Password123",
        )

        self.assertFalse(ok)
        self.assertEqual(flow.result.password, "Created!Password123")
        flow._on_password.assert_called_once_with(
            "passwordless@example.com", "Created!Password123"
        )

    def test_business_invalid_auth_step_on_http_200_is_failure(self):
        flow = _flow(_Response(200, {"code": "invalid_auth_step"}))

        ok = flow.create_password_via_api(
            "passwordless@example.com",
            "Created!Password123",
        )

        self.assertFalse(ok)
        self.assertEqual(flow.result.password, "Created!Password123")
        flow._on_password.assert_called_once_with(
            "passwordless@example.com", "Created!Password123"
        )

    def test_plain_text_error_on_http_200_is_failure(self):
        flow = _flow(_Response(200, text="Invalid authorization step."))

        ok = flow.create_password_via_api(
            "passwordless@example.com",
            "Created!Password123",
        )

        self.assertFalse(ok)
        self.assertEqual(flow.result.password, "Created!Password123")
        flow._on_password.assert_called_once_with(
            "passwordless@example.com", "Created!Password123"
        )

    def test_callback_continuation_still_opens_registration_password_step(self):
        flow = _flow(_Response(200, {}))
        flow._password_creation_continue_url = (
            "https://chatgpt.com/api/auth/callback/openai?code=one-time"
        )

        ok = flow.create_password_via_api(
            "passwordless@example.com",
            "Created!Password123",
        )

        self.assertTrue(ok)
        flow.session.get.assert_called_once()
        self.assertEqual(
            flow.session.get.call_args.args[0],
            "https://auth.openai.com/create-account/password",
        )
        flow.session.post.assert_called_once()
        self.assertNotIn(
            "reset-password",
            flow.session.get.call_args.args[0].lower(),
        )

    def test_reset_password_redirect_is_rejected_before_user_register(self):
        response = _Response(
            302,
            text="",
        )
        response.headers = {"Location": "https://auth.openai.com/reset-password/new-password"}
        flow = _flow(_Response(200, {}))
        flow.session.get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "reset-password"):
            flow.create_password_via_api(
                "passwordless@example.com",
                "Created!Password123",
            )
        flow.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
