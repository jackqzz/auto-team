import base64
import hashlib
import io
import json
import unittest
import zipfile

from webui import export_formats


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
             "id_token", "last_refresh", "refresh_token", "type"],
        )
        self.assertEqual(data["account_id"], "account-123")
        self.assertFalse(data["disabled"])
        self.assertEqual(data["type"], "codex")

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

    def test_sub2_batch_matches_import_file_shape(self):
        row = _row()
        fmt = export_formats.get_format("sub2api")
        data = json.loads(export_formats.render_bytes([row], fmt))
        self.assertEqual(fmt.filename_for([row]), "sub2api-accounts-remaining-1.json")
        self.assertEqual(list(data), ["type", "version", "exported_at", "workspace_id", "proxies", "accounts"])
        self.assertEqual(data["type"], "sub2api-data")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["workspace_id"], "account-123")
        self.assertEqual(data["proxies"], [])
        account = data["accounts"][0]
        self.assertEqual(account["name"], "user@example.com")
        self.assertEqual(account["platform"], "openai")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(list(account), [
            "name", "platform", "type", "credentials", "extra", "group_ids",
            "priority", "concurrency", "rate_multiplier", "auto_pause_on_expired",
        ])
        self.assertEqual(account["credentials"]["chatgpt_account_id"], "account-123")
        self.assertEqual(account["credentials"]["email"], "user@example.com")
        self.assertEqual(account["credentials"]["password"], "GptPass!")
        self.assertEqual(account["credentials"]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(account["credentials"]["plan_type"], "team")
        self.assertEqual(account["extra"]["source"], "workspace_oauth")
        self.assertEqual(account["extra"]["workspace_id"], "account-123")
        self.assertEqual(account["group_ids"], [4])
        self.assertNotIn("notes", account)
        self.assertNotIn("organization_id", account["credentials"])


if __name__ == "__main__":
    unittest.main()
