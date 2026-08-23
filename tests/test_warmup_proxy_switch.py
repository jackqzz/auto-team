import unittest
from types import SimpleNamespace
from unittest.mock import patch

import auth_flow
from auth_flow import AuthFlow
from webui.auto_loop import AutoLoopController


class _Cookies:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_dict(self):
        return dict(self.values)


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _Session:
    def __init__(self, status_code, cookies=None):
        self.status_code = status_code
        self.cookies = _Cookies(cookies)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response(self.status_code)


class WarmupProxySwitchTests(unittest.TestCase):
    def test_warmup_403_switches_proxy_and_continues_same_flow(self):
        first = _Session(403, {"__cf_bm": "blocked"})
        second = _Session(200, {"oai-did": "device"})
        callback_calls = []

        flow = AuthFlow.__new__(AuthFlow)
        flow.config = SimpleNamespace(proxy="http://proxy-one")
        flow.session = first
        flow._on_proxy_switch = lambda current, reason: (
            callback_calls.append((current, reason)) or "http://proxy-two"
        )
        flow._country_code = "US"
        flow._impersonate_candidates = ["chrome"]
        flow._impersonate_idx = 0
        flow._ua = "test-agent"
        flow._navigation_headers = lambda: {}
        flow.check_proxy = lambda: True

        with patch.object(auth_flow, "create_http_session", return_value=second) as create:
            self.assertTrue(flow.warmup())

        self.assertEqual(callback_calls[0][0], "http://proxy-one")
        self.assertIn("HTTP 403", callback_calls[0][1])
        self.assertEqual(flow.config.proxy, "http://proxy-two")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)
        create.assert_called_once()

    def test_proxy_pool_retry_avoids_failed_proxy(self):
        controller = AutoLoopController()
        controller._proxy_pool = ["proxy-one", "proxy-two", "proxy-three"]
        controller._proxy_usage = [0, 2, 1]

        selected = controller._proxy_for_worker(exclude_proxy="proxy-one")

        self.assertEqual(selected, "proxy-three")
        self.assertEqual(controller._proxy_usage, [0, 2, 2])

    def test_single_dynamic_proxy_can_be_released_again(self):
        controller = AutoLoopController()
        controller._proxy_pool = ["sid-dynamic-proxy"]
        controller._proxy_usage = [1]

        selected = controller._proxy_for_worker(exclude_proxy="sid-dynamic-proxy")

        self.assertEqual(selected, "sid-dynamic-proxy")
        self.assertEqual(controller._proxy_usage, [2])


if __name__ == "__main__":
    unittest.main()
