import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from webui.two_factor import totp_is_enabled


def _flow(payload, status_code=200):
    response = SimpleNamespace(
        status_code=status_code,
        text="",
        json=Mock(return_value=payload),
    )
    return SimpleNamespace(
        result=SimpleNamespace(access_token="access"),
        _common_headers=Mock(return_value={}),
        session=SimpleNamespace(get=Mock(return_value=response)),
    )


class TotpStatusTests(unittest.TestCase):
    def test_detects_totp_object_factor(self):
        flow = _flow({"mfa_enabled": True, "factors": {"totp": {"id": "factor"}}})
        self.assertTrue(totp_is_enabled(flow))

    def test_detects_empty_totp_object_factor(self):
        flow = _flow({"mfa_enabled": True, "factors": {"totp": {}}})
        self.assertTrue(totp_is_enabled(flow))

    def test_detects_mfa_v2_flag(self):
        flow = _flow({"mfa_enabled_v2": True, "factors": {"totp": {}}})
        self.assertTrue(totp_is_enabled(flow))

    def test_detects_totp_list_factor(self):
        flow = _flow({"mfa_enabled": "true", "factors": [{"factor_type": "totp"}]})
        self.assertTrue(totp_is_enabled(flow))

    def test_reports_explicitly_disabled_totp(self):
        flow = _flow({"mfa_enabled": False, "factors": {"totp": None}})
        self.assertFalse(totp_is_enabled(flow))

    def test_unknown_response_returns_none(self):
        flow = _flow(["unexpected"])
        self.assertIsNone(totp_is_enabled(flow))


if __name__ == "__main__":
    unittest.main()
