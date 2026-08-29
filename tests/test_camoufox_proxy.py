import unittest
from types import SimpleNamespace

from auth_flow import AuthFlow


def _flow(proxy: str | None) -> AuthFlow:
    flow = AuthFlow.__new__(AuthFlow)
    flow.config = SimpleNamespace(proxy=proxy)
    return flow


class CamoufoxProxyConfigTests(unittest.TestCase):
    def test_authenticated_socks5_uses_http_connect_and_separate_credentials(self):
        config, shown = _flow(
            "socks5://user-name:p%40ss@example.test:6066"
        )._camoufox_proxy_config()

        self.assertEqual(
            config,
            {
                "server": "http://example.test:6066",
                "username": "user-name",
                "password": "p@ss",
            },
        )
        self.assertEqual(shown, "http://<credentials>@example.test:6066")
        self.assertNotIn("user-name", shown)
        self.assertNotIn("p@ss", shown)

    def test_unauthenticated_socks5h_remains_socks5(self):
        config, shown = _flow(
            "socks5h://127.0.0.1:1080"
        )._camoufox_proxy_config()

        self.assertEqual(config, {"server": "socks5://127.0.0.1:1080"})
        self.assertEqual(shown, "socks5://127.0.0.1:1080")

    def test_authenticated_http_proxy_separates_credentials(self):
        config, shown = _flow(
            "http://alice:secret@proxy.example:3128"
        )._camoufox_proxy_config()

        self.assertEqual(
            config,
            {
                "server": "http://proxy.example:3128",
                "username": "alice",
                "password": "secret",
            },
        )
        self.assertEqual(shown, "http://<credentials>@proxy.example:3128")

    def test_empty_proxy_uses_direct_connection(self):
        self.assertEqual(_flow(None)._camoufox_proxy_config(), (None, "直连"))


if __name__ == "__main__":
    unittest.main()
