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

    def test_workspace_sub2_export_passes_external_workspace_id_to_renderer(self):
        from webui import exporter

        access = "access-token"
        identity = self._jwt({"at_hash": exporter._oidc_at_hash(access)})
        workspace = [{
            "email": "candidate@example.com",
            "access_token": access,
            "refresh_token": "old-refresh",
            "id_token": identity,
            "password": "GptPass!",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }]
        request = app.ExportRegisteredReq(
            format="sub2api",
            emails=["candidate@example.com"],
            workspace_id=2,
            refresh_oauth=False,
        )
        with (
            patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace),
            patch.object(app.db, "get_workspace_master", return_value={"workspace_id": "external-workspace-2"}),
            patch.object(app.export_formats, "render_bytes", return_value=b"{}") as render,
        ):
            app.api_export_registered(request)

        self.assertEqual(render.call_args.kwargs["workspace_id"], "external-workspace-2")
        self.assertTrue(render.call_args.kwargs["encrypt_credentials"])

    def test_workspace_cpa_and_sub2_share_explicit_plaintext_switch(self):
        from webui import exporter

        access = "access-token"
        identity = self._jwt({"at_hash": exporter._oidc_at_hash(access)})
        workspace = [{
            "email": "candidate@example.com",
            "access_token": access,
            "refresh_token": "refresh-token",
            "id_token": identity,
            "password": "OpenAI-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }]
        for format_id in ("cpa", "sub2api"):
            with self.subTest(format_id=format_id):
                request = app.ExportRegisteredReq(
                    format=format_id,
                    emails=["candidate@example.com"],
                    workspace_id=2,
                    refresh_oauth=False,
                    encrypt_credentials=False,
                )
                with (
                    patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace),
                    patch.object(app.db, "get_workspace_master", return_value={"workspace_id": "external-workspace-2"}),
                    patch.object(app.export_formats, "render_bytes", return_value=b"{}") as render,
                ):
                    app.api_export_registered(request)

                self.assertEqual(render.call_args.kwargs, {
                    "workspace_id": "external-workspace-2",
                    "encrypt_credentials": False,
                })

    def test_workspace_sub2_export_can_mark_candidates_outbound_after_render(self):
        from webui import exporter

        access = "access-token"
        identity = self._jwt({"at_hash": exporter._oidc_at_hash(access)})
        workspace = [{
            "email": "candidate@example.com",
            "access_token": access,
            "refresh_token": "refresh-token",
            "id_token": identity,
            "password": "OpenAI-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }]
        request = app.ExportRegisteredReq(
            format="sub2api",
            emails=["candidate@example.com"],
            workspace_id=2,
            encrypt_credentials=True,
            mark_outbound=True,
        )
        with (
            patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace),
            patch.object(app.db, "get_workspace_master", return_value={"workspace_id": "external-workspace-2"}),
            patch.object(app.export_formats, "render_bytes", return_value=b"encrypted-file") as render,
            patch.object(app.db, "update_workspace_candidate_tag_status", return_value=1) as mark,
        ):
            response = app.api_export_registered(request)

        self.assertEqual(response["outbound_marked"], 1)
        mark.assert_called_once_with(2, ["candidate@example.com"], "outbound")
        self.assertEqual(render.call_args.kwargs, {
            "workspace_id": "external-workspace-2",
            "encrypt_credentials": True,
        })

    def test_workspace_export_outbound_endpoint_encrypts_then_marks(self):
        from webui import credential_crypto, exporter

        access = self._jwt({
            "exp": 1788085162,
            "client_id": exporter.CODEX_CLIENT_ID,
            "https://api.openai.com/auth": {"chatgpt_account_id": "external-workspace-2"},
            "https://api.openai.com/profile": {"email": "candidate@example.com"},
        })
        identity = self._jwt({"at_hash": exporter._oidc_at_hash(access)})
        workspace_row = [{
            "email": "candidate@example.com",
            "access_token": access,
            "refresh_token": "refresh-token",
            "id_token": identity,
            "password": "OpenAI-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }]
        candidate_row = [{
            "email": "candidate@example.com",
            "account_status": "active",
            "trash_status": "active",
            "has_workspace_access_token": 1,
        }]
        request = app.WorkspaceExportOutboundReq(
            workspace_id=2,
            emails=["candidate@example.com"],
        )
        with (
            patch.object(app.db, "list_workspace_candidate_options", return_value=candidate_row),
            patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace_row),
            patch.object(app.db, "get_workspace_master", return_value={"workspace_id": "external-workspace-2"}),
            patch.object(app.db, "update_workspace_candidate_tag_status", return_value=1) as mark,
        ):
            result = app.api_workspace_candidates_export_outbound(request)

        self.assertEqual(result["outbound_marked"], 1)
        mark.assert_called_once_with(2, ["candidate@example.com"], "outbound")
        exported = json.loads(base64.b64decode(result["b64"]))
        credentials = exported["accounts"][0]["credentials"]
        self.assertEqual(
            credential_crypto.decrypt_credential(credentials["password"], "external-workspace-2"),
            "OpenAI-password",
        )
        self.assertEqual(
            credential_crypto.decrypt_credential(credentials["totp_secret"], "external-workspace-2"),
            "JBSWY3DPEHPK3PXP",
        )

    def test_mark_outbound_is_not_written_when_export_generation_fails(self):
        from webui import exporter

        access = "access-token"
        identity = self._jwt({"at_hash": exporter._oidc_at_hash(access)})
        workspace = [{
            "email": "candidate@example.com",
            "access_token": access,
            "refresh_token": "refresh-token",
            "id_token": identity,
        }]
        request = app.ExportRegisteredReq(
            format="sub2api",
            emails=["candidate@example.com"],
            workspace_id=2,
            encrypt_credentials=True,
            mark_outbound=True,
        )
        with (
            patch.object(app.db, "list_workspace_credentials_by_emails", return_value=workspace),
            patch.object(app.export_formats, "render_bytes", side_effect=RuntimeError("render failed")),
            patch.object(app.db, "update_workspace_candidate_tag_status") as mark,
        ):
            with self.assertRaises(Exception):
                app.api_export_registered(request)
        mark.assert_not_called()

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
