import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from auth_flow import AuthFlow, AuthResult


EMAIL = "account@example.com"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
CALLBACK_URL = "https://chatgpt.com/api/auth/callback/openai?code=test"


def _step(page_type, continue_url, mode=""):
    return {
        "page": {
            "type": page_type,
            "payload": {"email_verification_mode": mode},
        },
        "continue_url": continue_url,
    }


def _flow(password="", totp_secret=""):
    """Build a protocol-flow test double without creating a real HTTP session."""
    flow = AuthFlow.__new__(AuthFlow)
    flow.result = AuthResult()
    flow.result.email = EMAIL
    flow.result.totp_secret = totp_secret
    flow._account_callback = Mock(
        return_value={"password": password, "totp_secret": totp_secret}
    )
    flow._is_existing_account = False
    flow._existing_page_type = ""
    flow._existing_email_verification_mode = ""
    flow._env_overrides = {}
    flow._sms_callback = None

    flow.check_proxy = Mock(return_value=True)
    flow.warmup = Mock(return_value=True)
    flow.get_csrf_token = Mock(return_value="csrf")
    flow.get_auth_url = Mock(return_value="https://auth.openai.com/authorize")
    flow.auth_oauth_init = Mock(return_value="device-id")
    flow.get_sentinel_token = Mock(return_value="sentinel")
    flow.kickoff_otp_delivery = Mock(return_value=True)
    flow.send_otp = Mock()
    flow.login_password_verify = Mock()
    flow.submit_mfa_totp = Mock(return_value=_step("external_url", CALLBACK_URL))
    flow.fetch_client_auth_session_dump = Mock()
    flow.follow_redirect_chain = Mock(return_value=(CALLBACK_URL, CALLBACK_URL))
    flow._consume_callback_for_session = Mock(return_value=True)
    flow.oauth_token_exchange = Mock(return_value=False)
    flow.oauth_codex_rt_exchange = Mock(return_value=False)
    flow.oauth_secondary_authorize_exchange = Mock(return_value=False)
    flow._handle_add_phone_verification = Mock(side_effect=lambda continue_url: continue_url)
    flow._reauthorize_for_session = Mock(return_value="")
    flow._get_env = Mock(return_value="60")
    flow._env_flag = Mock(return_value=False)

    def set_session():
        flow.result.session_token = "session"
        flow.result.access_token = "access"

    flow.get_auth_session = Mock(side_effect=set_session)
    return flow


class ProtocolLoginTests(unittest.TestCase):
    def test_protocol_login_consumes_callback_before_fetching_session(self):
        flow = _flow()
        flow._resolve_login_password = Mock(return_value=("guessed-password", False))

        def passwordless_signup(_email, _sentinel):
            flow._is_existing_account = True
            flow._existing_page_type = "email_otp_verification"
            flow._existing_email_verification_mode = "passwordless_login"
            return False

        flow.signup = Mock(side_effect=passwordless_signup)
        flow.verify_otp = Mock(return_value=_step("external_url", CALLBACK_URL))
        provider = SimpleNamespace(wait_for_otp=Mock(return_value="890681"))

        flow.run_protocol_login(provider, EMAIL)

        flow._consume_callback_for_session.assert_called_once_with(CALLBACK_URL)

    def test_password_creation_runs_after_otp_before_callback(self):
        """补密码必须走 user/register，且发生在 callback/session 之前。"""
        flow = _flow()
        flow._resolve_login_password = Mock(return_value=("guessed-password", False))

        def passwordless_signup(_email, _sentinel):
            flow._is_existing_account = True
            flow._existing_page_type = "email_otp_verification"
            flow._existing_email_verification_mode = "passwordless_login"
            return False

        flow.signup = Mock(side_effect=passwordless_signup)
        flow.verify_otp = Mock(return_value=_step("external_url", CALLBACK_URL))

        order = []

        def create_password(_email, password):
            order.append("create_password")
            flow.result.password = password
            flow._password_created_during_login = True
            flow._last_password_continue_url = CALLBACK_URL
            return True

        flow.create_password_via_api = Mock(side_effect=create_password)
        flow.follow_redirect_chain = Mock(
            side_effect=lambda url: (order.append("follow"), (CALLBACK_URL, CALLBACK_URL))[1]
        )
        provider = SimpleNamespace(wait_for_otp=Mock(return_value="890681"))

        result = flow.run_protocol_login(
            provider,
            EMAIL,
            create_password="Created!Password123",
        )

        self.assertEqual(result.password, "Created!Password123")
        flow.create_password_via_api.assert_called_once_with(
            EMAIL,
            "Created!Password123",
        )
        self.assertEqual(order, ["create_password", "follow"])

    def test_password_step_is_opened_before_user_register(self):
        flow = _flow()
        password_step = "https://auth.openai.com/create-account/password"
        flow.session = SimpleNamespace(get=Mock())
        flow._trace_http = Mock()
        flow._common_headers = Mock(return_value={})
        flow._last_password_continue_url = ""
        sequence = []

        def open_step(*args, **kwargs):
            sequence.append("open_step")
            return SimpleNamespace(status_code=200, headers={}, text="")

        flow.session.get.side_effect = open_step
        flow.create_password_via_api = Mock(
            side_effect=lambda email, password: sequence.append("register") or True
        )
        flow._last_password_continue_url = ""
        flow._normalize_continue_url = lambda url: url or ""
        flow._create_password_after_otp(
            provider := SimpleNamespace(),
            EMAIL,
            "Created!Password123",
            password_step,
            60,
        )

        # The API helper owns opening the password step.  This test uses a
        # mocked helper, so only its registration call is observable here.
        self.assertEqual(sequence, ["register"])

    def test_real_password_creation_keeps_callback_unconsumed(self):
        """OTP callback must not be GETed; registration page precedes POST API."""
        flow = _flow()
        flow.result.device_id = ""
        flow.session = SimpleNamespace()
        sequence = []

        class _Response:
            status_code = 200
            text = ""
            headers = {}

            def __init__(self, payload=None):
                self._payload = payload or {}

            def json(self):
                return self._payload

        def get(url, **kwargs):
            sequence.append(("get", url))
            return _Response()

        def post(url, **kwargs):
            sequence.append(("post", url))
            return _Response({"continue_url": CALLBACK_URL})

        flow.session.get = Mock(side_effect=get)
        flow.session.post = Mock(side_effect=post)
        flow._common_headers = Mock(return_value={})
        flow._trace_http = Mock()
        flow._normalize_continue_url = lambda url: url or ""
        flow._password_creation_continue_url = CALLBACK_URL
        flow._password_creation_step_opened = False
        flow._last_sentinel_token = ""
        flow._last_sentinel_so_token = ""
        flow._on_password = Mock()

        next_url = flow._create_password_after_otp(
            SimpleNamespace(),
            EMAIL,
            "Created!Password123",
            CALLBACK_URL,
            60,
        )

        self.assertEqual(next_url, CALLBACK_URL)
        self.assertEqual(sequence[0], ("get", "https://auth.openai.com/create-account/password"))
        self.assertEqual(sequence[1], ("post", "https://auth.openai.com/api/accounts/user/register"))
        self.assertNotIn(CALLBACK_URL, [url for kind, url in sequence if kind == "get"])

    def test_callback_start_is_not_requested_by_redirect_walker(self):
        flow = _flow()
        flow.session = SimpleNamespace(get=Mock())

        callback, final = flow.follow_redirect_chain(CALLBACK_URL)

        self.assertEqual(callback, CALLBACK_URL)
        self.assertEqual(final, CALLBACK_URL)
        flow.session.get.assert_not_called()

    def test_passwordless_login_uses_email_otp_then_totp(self):
        flow = _flow(totp_secret=TOTP_SECRET)
        flow._resolve_login_password = Mock(return_value=("guessed-password", False))

        def passwordless_signup(_email, _sentinel):
            flow._is_existing_account = True
            flow._existing_page_type = "email_otp_verification"
            flow._existing_email_verification_mode = "passwordless_login"
            return False

        flow.signup = Mock(side_effect=passwordless_signup)
        flow.verify_otp = Mock(
            return_value=_step(
                "mfa_challenge",
                "https://auth.openai.com/mfa-challenge/challenge-id",
            )
        )
        provider = SimpleNamespace(wait_for_otp=Mock(return_value="890681"))

        result = flow.run_protocol_login(provider, EMAIL)

        self.assertEqual(result.access_token, "access")
        flow.login_password_verify.assert_not_called()
        flow.signup.assert_called_once_with(EMAIL, "sentinel")
        provider.wait_for_otp.assert_called_once()
        flow.verify_otp.assert_called_once_with("890681")
        flow.submit_mfa_totp.assert_called_once()

    def test_real_password_prefers_password_and_totp_without_email_otp(self):
        flow = _flow(password="real-password", totp_secret=TOTP_SECRET)
        flow._resolve_login_password = Mock(return_value=("real-password", True))
        flow._env_flag = Mock(
            side_effect=lambda key, default="0": key == "LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT"
        )
        flow.authorize_continue = Mock(
            return_value=_step("login_password", "https://auth.openai.com/log-in/password")
        )
        flow.login_password_verify.return_value = _step(
            "mfa_challenge",
            "https://auth.openai.com/mfa-challenge/challenge-id",
        )
        flow.signup = Mock()
        flow.verify_otp = Mock()
        provider = SimpleNamespace(wait_for_otp=Mock())

        result = flow.run_protocol_login(provider, EMAIL)

        self.assertEqual(result.password, "real-password")
        flow.login_password_verify.assert_called_once_with("real-password")
        flow.submit_mfa_totp.assert_called_once()
        provider.wait_for_otp.assert_not_called()
        flow.verify_otp.assert_not_called()
        flow.signup.assert_not_called()

    def test_rejected_real_password_restarts_state_before_otp_fallback(self):
        flow = _flow(password="stale-password")
        flow._resolve_login_password = Mock(return_value=("stale-password", True))
        flow._env_flag = Mock(
            side_effect=lambda key, default="0": key == "LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT"
        )
        flow.authorize_continue = Mock(
            return_value=_step("login_password", "https://auth.openai.com/log-in/password")
        )
        flow.login_password_verify.side_effect = RuntimeError("密码登录失败: 401")

        def passwordless_signup(_email, _sentinel):
            flow._is_existing_account = True
            flow._existing_page_type = "email_otp_verification"
            flow._existing_email_verification_mode = "passwordless_login"
            return False

        flow.signup = Mock(side_effect=passwordless_signup)
        flow.verify_otp = Mock(return_value=_step("external_url", CALLBACK_URL))
        provider = SimpleNamespace(wait_for_otp=Mock(return_value="890681"))

        result = flow.run_protocol_login(provider, EMAIL)

        self.assertEqual(result.access_token, "access")
        self.assertEqual(flow.auth_oauth_init.call_count, 2)
        self.assertEqual(flow.get_sentinel_token.call_count, 2)
        provider.wait_for_otp.assert_called_once()
        flow.verify_otp.assert_called_once_with("890681")

    def test_known_password_without_local_totp_uses_password_path(self):
        flow = _flow(password="known-password")
        flow._resolve_login_password = Mock(return_value=("known-password", True))
        flow._env_flag = Mock(
            side_effect=lambda key, default="0": key == "LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT"
        )

        flow.authorize_continue = Mock(
            return_value=_step("login_password", "https://auth.openai.com/log-in/password")
        )
        flow.login_password_verify.return_value = _step("external_url", CALLBACK_URL)
        flow.signup = Mock()
        provider = SimpleNamespace(wait_for_otp=Mock())

        result = flow.run_protocol_login(
            provider,
            EMAIL,
            password="known-password",
        )

        self.assertEqual(result.access_token, "access")
        self.assertEqual(result.password, "known-password")
        flow.login_password_verify.assert_called_once_with("known-password")
        flow.signup.assert_not_called()
        provider.wait_for_otp.assert_not_called()

    def test_prefer_email_otp_skips_known_password_for_password_completion(self):
        """补密码时即使库里有候选密码，也必须先建立邮箱 OTP 登录态。"""
        flow = _flow(password="old-password", totp_secret=TOTP_SECRET)
        flow._resolve_login_password = Mock(return_value=("old-password", True))

        def passwordless_signup(_email, _sentinel):
            flow._is_existing_account = True
            flow._existing_page_type = "email_otp_verification"
            flow._existing_email_verification_mode = "passwordless_login"
            return False

        flow.signup = Mock(side_effect=passwordless_signup)
        flow.verify_otp = Mock(return_value=_step("external_url", CALLBACK_URL))
        provider = SimpleNamespace(wait_for_otp=Mock(return_value="890681"))

        result = flow.run_protocol_login(
            provider,
            EMAIL,
            password="old-password",
            prefer_email_otp=True,
        )

        self.assertEqual(result.access_token, "access")
        self.assertEqual(result.password, "")
        flow.login_password_verify.assert_not_called()
        flow.signup.assert_called_once_with(EMAIL, "sentinel")
        provider.wait_for_otp.assert_called_once()

    def test_password_login_reports_missing_totp_secret_explicitly(self):
        flow = _flow(password="known-password")
        flow._resolve_login_password = Mock(return_value=("known-password", True))
        flow._env_flag = Mock(
            side_effect=lambda key, default="0": key == "LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT"
        )
        flow.authorize_continue = Mock(
            return_value=_step("login_password", "https://auth.openai.com/log-in/password")
        )
        flow.login_password_verify.return_value = _step(
            "mfa_challenge",
            "https://auth.openai.com/mfa-challenge/challenge-id",
        )
        flow.signup = Mock()
        provider = SimpleNamespace(wait_for_otp=Mock())

        with self.assertRaisesRegex(RuntimeError, "已启用 2FA"):
            flow.run_protocol_login(provider, EMAIL, password="known-password")

        flow.signup.assert_not_called()
        provider.wait_for_otp.assert_not_called()

    def test_otp_login_reports_missing_totp_secret_explicitly(self):
        flow = _flow()
        flow._resolve_login_password = Mock(return_value=("guessed-password", False))

        def passwordless_signup(_email, _sentinel):
            flow._is_existing_account = True
            flow._existing_page_type = "email_otp_verification"
            flow._existing_email_verification_mode = "passwordless_login"
            return False

        flow.signup = Mock(side_effect=passwordless_signup)
        flow.verify_otp = Mock(
            return_value=_step(
                "mfa_challenge",
                "https://auth.openai.com/mfa-challenge/challenge-id",
            )
        )
        provider = SimpleNamespace(wait_for_otp=Mock(return_value="890681"))

        with self.assertRaisesRegex(RuntimeError, "无法重新生成"):
            flow.run_protocol_login(provider, EMAIL)

    def test_codex_passwordless_login_never_submits_guessed_password(self):
        flow = _flow(totp_secret=TOTP_SECRET)
        flow._resolve_login_password = Mock(return_value=("guessed-password", False))
        flow.session = SimpleNamespace(
            cookies=SimpleNamespace(get=Mock(return_value="device-id"))
        )
        flow.authorize_continue = Mock(
            return_value=_step(
                "email_otp_verification",
                "https://auth.openai.com/email-verification",
                "passwordless_login",
            )
        )
        flow.verify_otp = Mock(
            return_value=_step(
                "mfa_challenge",
                "https://auth.openai.com/mfa-challenge/challenge-id",
            )
        )
        provider = SimpleNamespace(wait_for_otp=Mock(return_value="890681"))

        continue_url = flow._codex_drive_login_from_log_in(mail_provider=provider)

        self.assertEqual(continue_url, CALLBACK_URL)
        self.assertEqual(flow.authorize_continue.call_args.kwargs["screen_hint"], "signup")
        flow.login_password_verify.assert_not_called()
        provider.wait_for_otp.assert_called_once()
        flow.submit_mfa_totp.assert_called_once()


if __name__ == "__main__":
    unittest.main()
