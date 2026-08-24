import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from auth_flow import AuthFlow, AuthResult
from http_client import create_http_session


class _FakePhoneController:
    provider_key = "fake"

    def __init__(self):
        self.config = {
            "sms_max_phone_attempts": 2,
            "sms_per_phone_timeout": 40,
            "sms_code_retries_per_phone": 1,
        }
        self.index = 0
        self.events = []

    def get_phone(self):
        self.index += 1
        self.events.append(("get_phone", self.index))
        return f"+100{self.index}"

    def cleanup(self):
        self.events.append(("cleanup", self.index))

    def mark_send_failed(self, reason=""):
        self.events.append(("send_failed", reason))

    def mark_send_succeeded(self):
        self.events.append(("send_succeeded",))

    def get_code(self, timeout=0):
        self.events.append(("get_code", timeout))
        return "123456"

    def mark_code_failed(self, reason=""):
        self.events.append(("code_failed", reason))

    def report_success(self):
        self.events.append(("report_success",))


def _flow_for_phone_tests():
    flow = AuthFlow.__new__(AuthFlow)
    flow._normalize_continue_url = lambda value: value or ""
    return flow


class AuthPhoneFlowTests(unittest.TestCase):
    def test_cookie_helpers_use_real_jar_and_prefer_chatgpt_domain(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow.session = create_http_session()
        flow.result = AuthResult()
        flow.result.session_token = "session-from-json"
        flow.session.cookies.set("oai-did", "chat-device", domain=".chatgpt.com", path="/")
        flow.session.cookies.set("oai-did", "auth-device", domain=".openai.com", path="/")

        self.assertEqual(flow._get_device_id_cookie(), "chat-device")
        self.assertEqual(flow._extract_session_cookie(), "")
        self.assertIn(
            "__Secure-next-auth.session-token=session-from-json",
            flow._build_chatgpt_cookie_header(),
        )

    def test_sms_change_number_reauthorizes_before_second_send(self):
        flow = _flow_for_phone_tests()
        ctrl = _FakePhoneController()
        flow._add_phone_send = Mock(
            side_effect=[
                RuntimeError("Invalid authorization step."),
                {
                    "page": {"type": "phone_otp_verification"},
                    "continue_url": "https://auth.openai.com/phone-verification",
                },
            ]
        )
        flow._phone_otp_validate = Mock(
            return_value={"continue_url": "https://chatgpt.com/api/auth/callback/openai"}
        )
        def _reauthorize():
            ctrl.events.append(("reauthorize",))
            return "https://auth.openai.com/add-phone"

        reauthorize = Mock(side_effect=_reauthorize)

        result = flow._do_sms_loop(ctrl, reauthorize_callback=reauthorize)

        self.assertEqual(result, "https://chatgpt.com/api/auth/callback/openai")
        self.assertEqual(reauthorize.call_count, 1)
        self.assertEqual(flow._add_phone_send.call_count, 2)
        self.assertEqual(
            [event[0] for event in ctrl.events],
            [
                "get_phone",
                "send_failed",
                "cleanup",
                "reauthorize",
                "get_phone",
                "send_succeeded",
                "get_code",
                "report_success",
            ],
        )

    def test_camoufox_selection_does_not_fall_back_to_api(self):
        flow = _flow_for_phone_tests()
        flow._sms_callback = object()
        flow._get_env = Mock(return_value="camoufox")
        flow._handle_add_phone_via_camoufox = Mock(return_value="browser-result")
        flow._handle_add_phone_via_sms = Mock(return_value="api-result")

        result = flow._handle_add_phone_verification("https://auth.openai.com/add-phone")

        self.assertEqual(result, "browser-result")
        flow._handle_add_phone_via_camoufox.assert_called_once()
        flow._handle_add_phone_via_sms.assert_not_called()

        flow._handle_add_phone_via_camoufox.side_effect = RuntimeError("browser failed")
        with self.assertRaisesRegex(RuntimeError, "browser failed"):
            flow._handle_add_phone_verification("https://auth.openai.com/add-phone")
        flow._handle_add_phone_via_sms.assert_not_called()

    def test_reauthorization_repeats_totp_but_stops_before_phone_handler(self):
        flow = _flow_for_phone_tests()
        flow.result = AuthResult()
        flow.result.email = "account@example.com"
        flow.result.device_id = "device-id"
        flow.session = SimpleNamespace(
            cookies=SimpleNamespace(get=Mock(return_value="device-id"))
        )
        flow._account_callback = Mock(
            return_value={
                "password": "real-password",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            }
        )
        flow._resolve_login_password = Mock(return_value=("real-password", True))
        flow.get_sentinel_token = Mock(return_value="sentinel")
        flow.authorize_continue = Mock(
            return_value={
                "page": {"type": "login_password"},
                "continue_url": "https://auth.openai.com/log-in/password",
            }
        )
        flow.login_password_verify = Mock(
            return_value={
                "page": {"type": "mfa_challenge"},
                "continue_url": "https://auth.openai.com/mfa-challenge/challenge-id",
            }
        )
        flow.submit_mfa_totp = Mock(
            return_value={
                "page": {"type": "add_phone"},
                "continue_url": "https://auth.openai.com/add-phone",
            }
        )
        flow._handle_add_phone_verification = Mock()

        result = flow._codex_drive_login_from_log_in(handle_add_phone=False)

        self.assertEqual(result, "https://auth.openai.com/add-phone")
        flow.login_password_verify.assert_called_once_with("real-password")
        flow.submit_mfa_totp.assert_called_once()
        flow._handle_add_phone_verification.assert_not_called()

    def test_phone_reauthorization_builds_fresh_oauth_before_login(self):
        flow = _flow_for_phone_tests()
        flow.result = AuthResult()
        flow.result.email = "account@example.com"
        flow.get_csrf_token = Mock(return_value="fresh-csrf")
        flow.get_auth_url = Mock(return_value="https://auth.openai.com/fresh-authorize")
        flow.auth_oauth_init = Mock(return_value="fresh-device")
        flow._codex_drive_login_from_log_in = Mock(
            return_value="https://auth.openai.com/add-phone"
        )

        result = flow._reauthorize_for_add_phone(mail_provider="provider")

        self.assertEqual(result, "https://auth.openai.com/add-phone")
        flow.get_auth_url.assert_called_once_with(
            "fresh-csrf", email="account@example.com"
        )
        flow.auth_oauth_init.assert_called_once_with(
            "https://auth.openai.com/fresh-authorize"
        )
        flow._codex_drive_login_from_log_in.assert_called_once_with(
            mail_provider="provider",
            handle_add_phone=False,
        )


if __name__ == "__main__":
    unittest.main()
