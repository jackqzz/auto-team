import base64
import hashlib
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from webui import export_formats, exporter
from webui.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
    is_encrypted_credential,
)


def _jwt(payload):
    enc = lambda obj: base64.urlsafe_b64encode(
        json.dumps(obj, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(payload)}.signature"


def _row(email="user@example.com", account_id="account-123", plan="team"):
    auth = {
        "chatgpt_account_id": account_id,
        "chatgpt_user_id": "user-123",
        "chatgpt_plan_type": plan,
        "organizations": [{"id": "org-123"}],
    }
    access = _jwt({
        "exp": 1788085162,
        "sub": "auth0|123",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "https://api.openai.com/auth": auth,
        "https://api.openai.com/profile": {"email": email},
    })
    identity = _jwt({"https://api.openai.com/auth": auth})
    return {
        "email": email,
        "password": "GptPass!",
        "access_token": access,
        "refresh_token": "rt.test",
        "id_token": identity,
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "mail_kind": "icloud_relay",
        "relay_url": "https://mail.example/code?id=1",
        "mail_password": "",
        "mail_client_id": "",
        "mail_refresh_token": "",
        "pool_kind": "icloud_relay",
    }


class ExportFormatTests(unittest.TestCase):
    def test_cpa_token_json_carries_password_and_totp_secret(self):
        row = _row()
        data = exporter.build_cpa_token_json(row)
        self.assertEqual(data["password"], row["password"])
        self.assertEqual(data["totp_secret"], row["totp_secret"])

    def test_cpa_token_json_reads_password_and_totp_from_notes_variants(self):
        row = _row()
        row.pop("password")
        row.pop("totp_secret")
        row["notes"] = json.dumps({
            "gpt": {"password": "notes-password"},
            "two_factor": {"secret": "notes-totp"},
        })
        data = exporter.build_cpa_token_json(row)
        self.assertEqual(data["password"], "notes-password")
        self.assertEqual(data["totp_secret"], "notes-totp")

    def test_cpa_token_json_accepts_json_encoded_credentials(self):
        row = _row()
        row.pop("password")
        row.pop("totp_secret")
        row["credentials"] = json.dumps({
            "password": "nested-password",
            "totp_secret": "nested-totp",
        })
        data = exporter.build_cpa_token_json(row)
        self.assertEqual(data["password"], "nested-password")
        self.assertEqual(data["totp_secret"], "nested-totp")

    def test_sub2_panel_push_stops_when_rt_refresh_fails(self):
        with patch.object(exporter, "refresh_codex_token", side_effect=RuntimeError("refresh rejected")):
            with self.assertRaisesRegex(RuntimeError, "已停止导出"):
                exporter.export_to_sub2api(
                    _row(),
                    {
                        "sub2api_url": "https://sub2.example",
                        "sub2api_api_key": "key",
                        "refresh_oauth": True,
                    },
                )

    def test_sub2_rejects_mismatched_access_and_id_tokens(self):
        row = _row()
        row["id_token"] = _jwt({
            "at_hash": "belongs-to-another-access-token",
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-123"},
        })
        with self.assertRaisesRegex(RuntimeError, "at_hash"):
            export_formats.render_bytes([row], "sub2api")

    def test_sub2_accepts_matching_oidc_token_pair(self):
        row = _row()
        row["id_token"] = _jwt({
            "at_hash": exporter._oidc_at_hash(row["access_token"]),
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-123"},
        })
        data = json.loads(export_formats.render_bytes([row], "sub2api"))
        self.assertEqual(data["accounts"][0]["credentials"]["access_token"], row["access_token"])

    def test_cpa_single_matches_sample_shape_and_filename(self):
        row = _row()
        fmt = export_formats.get_format("cpa")
        blob = export_formats.render_bytes([row], fmt)
        data = json.loads(blob)
        digest = hashlib.sha256(b"account-123").hexdigest()[:8]
        self.assertEqual(fmt.filename_for([row]), f"codex-{digest}-user@example.com-team.json")
        self.assertEqual(
            list(data),
            ["access_token", "account_id", "disabled", "email", "expired",
             "id_token", "last_refresh", "refresh_token", "type", "password",
             "totp_secret"],
        )
        self.assertEqual(data["account_id"], "account-123")
        self.assertFalse(data["disabled"])
        self.assertEqual(data["type"], "codex")
        self.assertEqual(data["password"], row["password"])
        self.assertEqual(data["totp_secret"], row["totp_secret"])

    def test_cpa_multiple_is_zip_with_one_json_per_account(self):
        rows = [_row(), _row("second@example.com", "account-456", "free")]
        fmt = export_formats.get_format("cpa")
        blob = export_formats.render_bytes(rows, fmt)
        self.assertEqual(fmt.mime_for(rows), "application/zip")
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), 2)
            self.assertTrue(any(name.endswith("-user@example.com-team.json") for name in names))
            self.assertTrue(any(name.endswith("-second@example.com-free.json") for name in names))

    def test_cpa_workspace_export_encrypts_both_login_fields(self):
        row = _row()
        workspace_id = "workspace-key-123"
        data = json.loads(
            export_formats.render_bytes(
                [row],
                "cpa",
                workspace_id=workspace_id,
                encrypt_credentials=True,
            )
        )
        self.assertEqual(data["account_id"], workspace_id)
        self.assertTrue(is_encrypted_credential(data["password"]))
        self.assertTrue(is_encrypted_credential(data["totp_secret"]))
        self.assertEqual(decrypt_credential(data["password"], workspace_id), row["password"])
        self.assertEqual(decrypt_credential(data["totp_secret"], workspace_id), row["totp_secret"])

    def test_cpa_workspace_export_plain_switch_matches_sub2(self):
        row = _row()
        workspace_id = "workspace-key-123"
        cpa = json.loads(
            export_formats.render_bytes(
                [row], "cpa", workspace_id=workspace_id, encrypt_credentials=False,
            )
        )
        sub2 = json.loads(
            export_formats.render_bytes(
                [row], "sub2api", workspace_id=workspace_id, encrypt_credentials=False,
            )
        )
        self.assertEqual(cpa["password"], sub2["accounts"][0]["credentials"]["password"])
        self.assertEqual(cpa["totp_secret"], sub2["accounts"][0]["credentials"]["totp_secret"])

    def test_plain_switch_decrypts_previously_encrypted_values_in_both_formats(self):
        workspace_id = "workspace-key-123"
        row = _row()
        row["password"] = encrypt_credential(row["password"], workspace_id)
        row["totp_secret"] = encrypt_credential(row["totp_secret"], workspace_id)

        cpa = json.loads(export_formats.render_bytes(
            [row], "cpa", workspace_id=workspace_id, encrypt_credentials=False,
        ))
        sub2 = json.loads(export_formats.render_bytes(
            [row], "sub2api", workspace_id=workspace_id, encrypt_credentials=False,
        ))
        self.assertEqual(cpa["password"], "GptPass!")
        self.assertEqual(cpa["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(sub2["accounts"][0]["credentials"]["password"], "GptPass!")
        self.assertEqual(sub2["accounts"][0]["credentials"]["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_sub2_batch_matches_import_file_shape(self):
        row = _row()
        fmt = export_formats.get_format("sub2api")
        data = json.loads(export_formats.render_bytes([row], fmt))
        self.assertEqual(fmt.filename_for([row]), "sub2api-accounts-remaining-1.json")
        self.assertEqual(list(data), ["type", "version", "exported_at", "proxies", "accounts"])
        self.assertEqual(data["type"], "sub2api-data")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["proxies"], [])
        account = data["accounts"][0]
        self.assertEqual(account["name"], "user@example.com")
        self.assertEqual(account["platform"], "openai")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(list(account), [
            "name", "type", "extra", "platform", "priority", "plan_type",
            "concurrency", "credentials", "group_ids", "expires_at",
            "auto_pause_on_expired",
        ])
        self.assertEqual(account["credentials"]["chatgpt_account_id"], "account-123")
        self.assertEqual(account["credentials"]["email"], "user@example.com")
        self.assertEqual(account["credentials"]["password"], "GptPass!")
        self.assertEqual(account["credentials"]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(account["credentials"]["plan_type"], "team")
        self.assertEqual(account["extra"]["source"], "internal_resource_exchange")
        self.assertEqual(account["credentials"]["expired"], "2026-08-30T10:19:22Z")
        self.assertEqual(account["credentials"]["expires_at"], "2026-08-30T10:19:22Z")
        self.assertEqual(account["credentials"]["type"], "codex")
        self.assertEqual(account["credentials"]["account_id"], "account-123")
        self.assertEqual(account["group_ids"], [4])
        self.assertNotIn("notes", account)
        self.assertEqual(account["credentials"]["organization_id"], "org-123")

    def test_workspace_sub2_export_encrypts_password_and_totp_with_workspace_id(self):
        row = _row()
        workspace_id = "workspace-key-123"
        data = json.loads(
            export_formats.render_bytes([row], "sub2api", workspace_id=workspace_id)
        )
        credentials = data["accounts"][0]["credentials"]
        self.assertTrue(is_encrypted_credential(credentials["password"]))
        self.assertTrue(is_encrypted_credential(credentials["totp_secret"]))
        self.assertEqual(
            decrypt_credential(credentials["password"], workspace_id),
            row["password"],
        )
        self.assertEqual(
            decrypt_credential(credentials["totp_secret"], workspace_id),
            row["totp_secret"],
        )

    def test_non_workspace_sub2_export_keeps_legacy_plaintext_fields(self):
        row = _row()
        data = json.loads(export_formats.render_bytes([row], "sub2api"))
        credentials = data["accounts"][0]["credentials"]
        self.assertEqual(credentials["password"], row["password"])
        self.assertEqual(credentials["totp_secret"], row["totp_secret"])

    def test_workspace_sub2_export_can_explicitly_keep_plaintext_fields(self):
        row = _row()
        data = json.loads(
            export_formats.render_bytes(
                [row],
                "sub2api",
                workspace_id="workspace-key-123",
                encrypt_credentials=False,
            )
        )
        credentials = data["accounts"][0]["credentials"]
        self.assertEqual(credentials["password"], row["password"])
        self.assertEqual(credentials["totp_secret"], row["totp_secret"])


if __name__ == "__main__":
    unittest.main()
