import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from auth_flow import AuthFlow, AuthResult


class ExistingRegistrationBranchTests(unittest.TestCase):
    def _flow(self, *, allow_login=True, mode="passwordless_login"):
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow._existing_email_verification_mode = mode
        flow._existing_page_type = "email_otp_verification"
        flow._is_existing_account = False
        flow._on_session_ready = None
        flow.check_proxy = Mock(return_value=True)
        flow.warmup = Mock(return_value=True)
        flow.get_csrf_token = Mock(return_value="csrf")
        flow.get_auth_url = Mock(return_value="https://auth.openai.com/authorize")
        flow.auth_oauth_init = Mock(return_value="device")
        flow.get_sentinel_token = Mock(return_value="sentinel")
        flow.signup = Mock(side_effect=self._signup(flow, mode))
        flow._get_env = Mock(
            side_effect=lambda key, default="": "1" if key == "WEBUI_ALLOW_LOGIN" and allow_login else default
        )
        flow.run_protocol_login = Mock(return_value=AuthResult())
        flow.run_protocol_login.return_value.email = "existing@example.com"
        flow.run_protocol_login.return_value.access_token = "at"
        flow.run_protocol_login.return_value.session_token = "st"
        return flow

    @staticmethod
    def _signup(flow, mode):
        def _inner(_email, _sentinel):
            flow._is_existing_account = mode != "passwordless_signup"
            return False

        return _inner

    def test_existing_registration_switches_to_protocol_login(self):
        flow = self._flow()
        provider = SimpleNamespace(
            pooled=True,
            requires_password=True,
            kind="icloud_relay",
            create_mailbox=Mock(return_value="existing@example.com"),
        )

        result = flow.run_register(provider, ensure_credentials=True)

        flow.run_protocol_login.assert_called_once_with(
            provider, "existing@example.com",
        )
        self.assertEqual(result.access_token, "at")

    def test_passwordless_signup_still_uses_registration_path(self):
        flow = self._flow(mode="passwordless_signup")
        provider = SimpleNamespace(
            pooled=True,
            requires_password=False,
            kind="icloud_relay",
            create_mailbox=Mock(return_value="new@example.com"),
            wait_for_otp=Mock(return_value="123456"),
        )
        flow.register_password = Mock(return_value=False)
        flow.kickoff_otp_delivery = Mock(return_value=True)
        flow.send_otp = Mock()
        flow.verify_otp = Mock(return_value={})
        flow.fetch_client_auth_session_dump = Mock()
        flow.create_account = Mock(return_value="")
        flow._get_env = Mock(
            side_effect=lambda key, default="": {
                "WEBUI_ALLOW_LOGIN": "1",
                "OTP_TIMEOUT": "10",
            }.get(key, default)
        )
        flow.follow_redirect_chain = Mock(return_value=("", ""))
        flow.get_auth_session = Mock()
        flow.result.access_token = "at"
        flow.result.session_token = "st"
        flow.result.email = "new@example.com"
        flow._env_flag = Mock(return_value=False)

        # A passwordless signup enters the normal branch and therefore calls
        # register_password; it must not call run_protocol_login.
        flow.run_register(provider, ensure_credentials=True)
        flow.run_protocol_login.assert_not_called()
        flow.register_password.assert_called_once_with("new@example.com")

    def test_generic_existing_account_requires_explicit_completion_without_password(self):
        flow = self._flow()
        provider = SimpleNamespace(
            pooled=True,
            requires_password=True,
            kind="icloud_relay",
            create_mailbox=Mock(return_value="existing@example.com"),
        )
        with self.assertRaisesRegex(RuntimeError, "开启已有账号凭证补齐"):
            flow.run_register(provider, ensure_credentials=False)
        flow.run_protocol_login.assert_not_called()

    def test_existing_account_with_local_password_can_skip_completion(self):
        flow = self._flow()
        flow._account_callback = Mock(
            return_value={"password": "known-password", "totp_secret": ""}
        )
        provider = SimpleNamespace(
            pooled=True,
            requires_password=True,
            kind="icloud_relay",
            create_mailbox=Mock(return_value="existing@example.com"),
        )
        result = flow.run_register(provider, ensure_credentials=False)
        flow.run_protocol_login.assert_called_once_with(
            provider, "existing@example.com",
        )
        self.assertEqual(result.access_token, "at")


if __name__ == "__main__":
    unittest.main()
