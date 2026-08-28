import base64
import json
import unittest
from unittest.mock import patch

from webui import app, exporter, public_relogin


def _jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class PublicReloginExportRefreshTests(unittest.TestCase):
    def test_refresh_export_returns_one_coherent_oauth_triplet(self):
        access = _jwt({
            "exp": 1890000000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "workspace-1",
                "chatgpt_user_id": "user-1",
            },
        })
        identity = _jwt({"at_hash": exporter._oidc_at_hash(access)})
        request = app.PublicReloginExportRefreshReq(
            account={
                "email": "candidate@example.com",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "id_token": "old-id",
                "password": "password",
                "totp_secret": "secret",
            },
            access_key="valid-key",
            proxy="socks5://proxy.example:1080",
        )
        with (
            patch.object(public_relogin, "get_effective_config", return_value={"enabled": True, "quota_timeout": 30}),
            patch.object(app, "_validate_public_relogin_access_key", return_value={}),
            patch.object(exporter, "refresh_codex_token", return_value={
                "access_token": access,
                "refresh_token": "rotated-refresh",
                "id_token": identity,
            }) as refresh,
        ):
            response = app.api_public_relogin_refresh_export(request)

        self.assertEqual(response["account"]["access_token"], access)
        self.assertEqual(response["account"]["refresh_token"], "rotated-refresh")
        self.assertEqual(response["account"]["id_token"], identity)
        self.assertEqual(response["account"]["expires_at"], 1890000000)
        self.assertEqual(refresh.call_args.kwargs["client_id"], exporter.CODEX_CLIENT_ID)
        self.assertEqual(refresh.call_args.kwargs["proxy"], "socks5://proxy.example:1080")


if __name__ == "__main__":
    unittest.main()
