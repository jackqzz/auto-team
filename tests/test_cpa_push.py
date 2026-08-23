import unittest
from unittest.mock import patch

from webui import exporter


class CpaPushTests(unittest.TestCase):
    def test_bulk_push_continues_after_one_account_fails(self):
        rows = [
            {"email": "one@example.com", "refresh_token": "rt-one"},
            {"email": "two@example.com", "refresh_token": ""},
        ]

        def fake_export(cred, **_kwargs):
            if cred["email"] == "one@example.com":
                return {"cpa": {"ok": True, "file_name": "one@example.com.json"}}
            return {"cpa": {"ok": False, "error": "缺少 refresh_token"}}

        with patch.object(exporter, "run_exports", side_effect=fake_export):
            results = exporter.push_many_to_cpa(
                rows, {"cpa_url": "https://cpa.example", "cpa_mgmt_key": "key"}
            )

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])


if __name__ == "__main__":
    unittest.main()
