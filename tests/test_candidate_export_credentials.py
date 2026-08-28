import unittest
import base64
import json
from unittest.mock import patch

try:
    from webui import app
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    app = None


@unittest.skipIf(app is None, "当前测试环境未安装 fastapi")
class CandidateExportCredentialTests(unittest.TestCase):
    @staticmethod
    def _jwt(payload):
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"header.{encoded}.signature"

    def test_workspace_sub2_refresh_writes_rotated_triplet_back_to_workspace(self):
        from webui import exporter

        fresh_access = "fresh-access"
        fresh_identity = self._jwt({"at_hash": exporter._oidc_at_hash(fresh_access)})
        workspace = [{
            "email": "candidate@example.com",
            "access_token": "stale-access",
            "refresh_token": "old-refresh",
            "id_token": "stale-id",
        }]
        request = app.ExportRegisteredReq(
            format="sub2api",
            emails=["candidate@example.com"],
            workspace_id=2,
            proxy_pool="socks5://proxy.example:1080",
            refresh_oauth=True,
        )
        with (
            patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace),
            patch.object(exporter, "refresh_codex_token", return_value={
                "access_token": fresh_access,
                "refresh_token": "rotated-refresh",
                "id_token": fresh_identity,
            }) as refresh,
            patch.object(app.db, "save_workspace_credential") as save_workspace,
            patch.object(app.db, "update_registered_oauth_tokens") as save_personal,
            patch.object(app.export_formats, "render_bytes", return_value=b"{}") as render,
            patch.object(app.public_relogin.proxy_usage, "record_lease"),
        ):
            app.api_export_registered(request)

        self.assertEqual(refresh.call_args.kwargs["client_id"], exporter.CODEX_CLIENT_ID)
        self.assertEqual(refresh.call_args.kwargs["proxy"], "socks5://proxy.example:1080")
        saved = save_workspace.call_args.args[1]
        self.assertEqual(saved["access_token"], fresh_access)
        self.assertEqual(saved["refresh_token"], "rotated-refresh")
        self.assertEqual(saved["id_token"], fresh_identity)
        save_personal.assert_not_called()
        self.assertEqual(render.call_args.args[0][0]["refresh_token"], "rotated-refresh")

    def test_sub2_refresh_failure_does_not_emit_stale_file(self):
        workspace = [{
            "email": "candidate@example.com",
            "access_token": "stale-access",
            "refresh_token": "refresh-token",
            "id_token": "stale-id",
        }]
        request = app.ExportRegisteredReq(
            format="sub2api",
            emails=["candidate@example.com"],
            workspace_id=2,
            refresh_oauth=True,
        )
        from webui import exporter

        with (
            patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace),
            patch.object(exporter, "refresh_codex_token", side_effect=RuntimeError("refresh rejected")),
            patch.object(app.export_formats, "render_bytes") as render,
        ):
            with self.assertRaises(Exception) as caught:
                app.api_export_registered(request)

        self.assertEqual(getattr(caught.exception, "status_code", None), 502)
        self.assertIn("refresh_token 刷新失败", str(getattr(caught.exception, "detail", caught.exception)))
        render.assert_not_called()

    def test_workspace_sub2_export_can_skip_refresh_and_use_existing_pair(self):
        from webui import exporter

        access = "access-token"
        identity = self._jwt({"at_hash": exporter._oidc_at_hash(access)})
        workspace = [{
            "email": "candidate@example.com",
            "access_token": access,
            "refresh_token": "old-refresh",
            "id_token": identity,
        }]
        request = app.ExportRegisteredReq(
            format="sub2api",
            emails=["candidate@example.com"],
            workspace_id=2,
            refresh_oauth=False,
        )
        with (
            patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace),
            patch.object(exporter, "refresh_codex_token") as refresh,
            patch.object(app.export_formats, "render_bytes", return_value=b"{}") as render,
        ):
            app.api_export_registered(request)

        refresh.assert_not_called()
        self.assertEqual(render.call_args.args[0][0]["access_token"], access)

    def test_text_credentials_use_registered_rows_with_workspace_id(self):
        registered = [{
            "email": "candidate@example.com",
            "password": "OpenAI-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }]
        workspace = [{
            "email": "candidate@example.com",
            "access_token": "workspace-token",
        }]
        request = app.ExportRegisteredReq(
            format="email_pw_2fa",
            emails=["candidate@example.com"],
            workspace_id=2,
        )
        with (
            patch.object(app.db, "list_registered_by_emails", return_value=registered) as registered_query,
            patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace) as workspace_query,
        ):
            response = app.api_export_registered(request)

        self.assertEqual(response["text"], "candidate@example.com----OpenAI-password----JBSWY3DPEHPK3PXP")
        registered_query.assert_called_once_with(["candidate@example.com"])
        workspace_query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
