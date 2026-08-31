import unittest
import asyncio
from unittest.mock import patch

try:
    from webui import app, public_relogin
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    app = None
    public_relogin = None


@unittest.skipIf(app is None, "当前测试环境未安装 fastapi")
class PublicReloginAutoReloginTests(unittest.TestCase):
    def test_public_relogin_access_key_is_temporarily_ignored(self):
        with patch.object(
            app.db,
            "validate_public_relogin_access_key",
            side_effect=AssertionError("public relogin must not validate the key"),
        ):
            self.assertEqual(app._validate_public_relogin_access_key("anything"), {})

    def test_check_401_starts_relogin_inside_check_worker(self):
        account = {
            "id": "account-1",
            "email": "user@example.com",
            "password": "password",
            "totp_secret": "secret",
            "chatgpt_account_id": "workspace-1",
        }
        refreshed = {
            "email": "user@example.com",
            "chatgpt_account_id": "workspace-1",
            "access_token": "new-token",
        }
        cfg = {
            "enabled": True,
            "concurrency": 2,
            "quota_timeout": 30,
            "login_timeout": 180,
            "retry_count": 0,
            "proxy_pool": "",
            "use_system_proxy_pool": True,
        }
        request = app.PublicReloginCheckReq(
            accounts=[account],
            access_key="valid-key",
            proxy_pool="proxy-one\nproxy-two",
            auto_relogin_on_401=True,
        )

        with (
            patch.object(public_relogin, "get_effective_config", return_value=cfg),
            patch.object(app, "_validate_public_relogin_access_key", return_value={}),
            patch.object(
                public_relogin,
                "fetch_quota",
                side_effect=public_relogin.PublicQuotaUnauthorized("HTTP 401"),
            ),
            patch.object(public_relogin, "relogin_account", return_value=refreshed) as relogin,
            patch.object(public_relogin.proxy_usage, "record_lease"),
        ):
            response = asyncio.run(app.api_public_relogin_check(request))

        result = response["results"]["account-1"]
        self.assertEqual(result["status"], "revived")
        self.assertTrue(result["detected_401"])
        self.assertEqual(result["account"]["access_token"], "new-token")
        # 查询使用第一条代理，立即复活应避开它并领取第二条。
        self.assertEqual(relogin.call_args.kwargs["proxy"], "proxy-two")

    def test_manual_check_does_not_auto_relogin(self):
        account = {
            "id": "account-1",
            "email": "user@example.com",
            "chatgpt_account_id": "workspace-1",
        }
        cfg = {
            "enabled": True,
            "concurrency": 1,
            "quota_timeout": 30,
            "login_timeout": 180,
            "retry_count": 0,
            "proxy_pool": "",
            "use_system_proxy_pool": True,
        }
        request = app.PublicReloginCheckReq(
            accounts=[account], access_key="valid-key", auto_relogin_on_401=False,
        )

        with (
            patch.object(public_relogin, "get_effective_config", return_value=cfg),
            patch.object(app, "_validate_public_relogin_access_key", return_value={}),
            patch.object(
                public_relogin,
                "fetch_quota",
                side_effect=public_relogin.PublicQuotaUnauthorized("HTTP 401"),
            ),
            patch.object(public_relogin, "relogin_account") as relogin,
        ):
            response = asyncio.run(app.api_public_relogin_check(request))

        self.assertEqual(response["results"]["account-1"]["status"], "401")
        relogin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
