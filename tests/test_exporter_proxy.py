import unittest
from unittest.mock import Mock, patch

from webui import exporter


class ExporterOAuthProxyTests(unittest.TestCase):
    def test_refresh_uses_explicit_proxy_argument_and_socks5h(self):
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "fresh-access"}
        cffi = Mock()
        cffi.post.return_value = response

        with patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.refresh_codex_token(
                "refresh-token",
                proxy=" socks5://user:pass@proxy.example:1080 ",
            )

        self.assertEqual(result["access_token"], "fresh-access")
        kwargs = cffi.post.call_args.kwargs
        self.assertEqual(kwargs["proxy"], "socks5h://user:pass@proxy.example:1080")
        self.assertNotIn("proxies", kwargs)

    def test_refresh_without_proxy_explicitly_uses_direct_connection(self):
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "fresh-access"}
        cffi = Mock()
        cffi.post.return_value = response

        with patch.object(exporter, "_import_cffi", return_value=cffi):
            exporter.refresh_codex_token("refresh-token")

        self.assertIsNone(cffi.post.call_args.kwargs["proxy"])

    def test_refresh_accepts_proxy_pool_config_as_compatibility_fallback(self):
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "fresh-access"}
        cffi = Mock()
        cffi.post.return_value = response

        with patch.object(exporter, "_import_cffi", return_value=cffi):
            exporter.refresh_codex_token(
                "refresh-token",
                proxy=exporter._proxy_from_config({
                    "proxy_pool": "proxy-one\nproxy-two",
                }),
            )

        self.assertEqual(cffi.post.call_args.kwargs["proxy"], "proxy-one")


if __name__ == "__main__":
    unittest.main()
