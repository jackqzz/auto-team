import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


class CandidateQuotaSeatTests(unittest.TestCase):
    def test_prolite_is_queryable_but_codex_is_excluded(self):
        self.assertFalse(app._is_non_default_seat("prolite"))
        self.assertFalse(app._is_non_default_seat("pro_lite"))
        self.assertTrue(app._is_non_default_seat("usage_based"))
        self.assertTrue(app._is_non_default_seat("Codex席位"))

    def _joined_row(self, seat_label=""):
        return {
            "account_status": "active",
            "trash_status": "active",
            "tag_status": "active",
            "workspace_join_status": "joined",
            "has_workspace_access_token": True,
            "seat_label": seat_label,
            "seat_type": "",
        }

    def test_unknown_seat_blocked_for_auto_but_queryable_manually(self):
        row = self._joined_row("")
        self.assertEqual(
            app._candidate_quota_ineligible_reason(row),
            "席位类型未知，暂不参与额度查询",
        )
        # 手动口径：wham/usage 不依赖席位类型，席位未知也可查询。
        self.assertEqual(
            app._candidate_quota_ineligible_reason(row, strict_seat=False), ""
        )

    def test_unrecognized_seat_blocked_for_auto_but_queryable_manually(self):
        row = self._joined_row("some_future_seat")
        self.assertEqual(
            app._candidate_quota_ineligible_reason(row),
            "席位类型未知，暂不参与额度查询",
        )
        self.assertEqual(
            app._candidate_quota_ineligible_reason(row, strict_seat=False), ""
        )

    def test_codex_seat_blocked_for_both(self):
        row = self._joined_row("Codex席位")
        self.assertEqual(
            app._candidate_quota_ineligible_reason(row), "Codex席位不参与额度查询"
        )
        self.assertEqual(
            app._candidate_quota_ineligible_reason(row, strict_seat=False),
            "Codex席位不参与额度查询",
        )


class QuotaZeroFilterTests(unittest.TestCase):
    """候选列表 quota_status=zero 筛选与 _is_zero_quota_payload 同口径。"""

    def test_zero_quota_filter(self):
        quotas = {
            "a@example.com": {"primary": {"used_percent": 100, "window_seconds": 18000}},
            "b@example.com": {"primary": {"used_percent": 40, "window_seconds": 604800}},
            "c@example.com": {"primary": {"used_percent": 12, "window_seconds": 18000},
                              "credits_balance": 0},
            "d@example.com": {"error_code": "401", "error": "unauthorized"},
            # e@example.com 不写 quota_json —— 没查过额度不算耗尽
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                db.import_workspace_sessions(
                    "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
                    "----socks5://127.0.0.1:1080"
                )
                for email in quotas.keys() | {"e@example.com"}:
                    db.save_registered({"email": email, "access_token": "token"})
                    db.save_workspace_credential(
                        1, {"email": email, "access_token": "ws-token"}
                    )
                    if email in quotas:
                        db.update_workspace_quota(1, email, quotas[email])

                rows = db.list_workspace_candidate_options(1, quota_status="zero")
                self.assertEqual(
                    sorted(r["email"] for r in rows),
                    ["a@example.com", "c@example.com"],
                )
                self.assertEqual(
                    db.count_workspace_candidate_options(1, quota_status="zero"), 2
                )
                with self.assertRaises(ValueError):
                    db.list_workspace_candidate_options(1, quota_status="bogus")


if __name__ == "__main__":
    unittest.main()
