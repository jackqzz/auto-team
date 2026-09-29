"""额度耗尽入箱可以选择按 5 小时限制还是周限制判定。

上游 ``wham/usage`` 的 ``primary``/``secondary`` 并不固定对应 5h/周：实测多数
账号只返回一个周窗口，而它落在 ``primary`` 上；只有同时受 5h 和周限制时
``primary`` 才是 5h。所以窗口种类只能按 ``limit_window_seconds`` 认，不能按
字段名认 —— 否则选"仅看 5h"会意外命中周额度。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


FIVE_HOUR = 18000
WEEKLY = 604800


def _payload(primary=None, secondary=None, **extra):
    """构造一条 fetch_candidate_quota 落库形状的额度记录。"""
    def window(spec):
        if spec is None:
            return {"used_percent": None, "window_seconds": None, "reset_at": None}
        seconds, used = spec
        return {"used_percent": used, "window_seconds": seconds, "reset_at": 0}

    return {
        "plan_type": "team",
        "credits_balance": None,
        "allowed": False,
        "primary": window(primary),
        "secondary": window(secondary),
        "updated_at": 0,
        **extra,
    }


class ZeroQuotaWindowSelectionTests(unittest.TestCase):
    def test_five_hour_exhausted_does_not_trash_when_weekly_selected(self):
        """5h 用尽但周额度还有：选周口径时不该入箱。"""
        payload = _payload(primary=(FIVE_HOUR, 100), secondary=(WEEKLY, 16))
        self.assertFalse(app._is_zero_quota_payload(payload, "weekly"))
        self.assertTrue(app._is_zero_quota_payload(payload, "five_hour"))
        # 历史口径任一窗口耗尽即算，保持不变。
        self.assertTrue(app._is_zero_quota_payload(payload, "any"))

    def test_weekly_exhausted_does_not_trash_when_five_hour_selected(self):
        payload = _payload(primary=(FIVE_HOUR, 20), secondary=(WEEKLY, 100))
        self.assertFalse(app._is_zero_quota_payload(payload, "five_hour"))
        self.assertTrue(app._is_zero_quota_payload(payload, "weekly"))
        self.assertTrue(app._is_zero_quota_payload(payload, "any"))

    def test_window_kind_is_read_from_seconds_not_field_order(self):
        """周窗口落在 primary 上时，选"周"必须命中它，选"5h"必须放过它。

        这是线上最常见的形状（90 条已存额度里 75 条如此）。若按字段名认窗口，
        选"仅看 5h"会把整批只有周限制的账号误判成 5h 用尽。
        """
        payload = _payload(primary=(WEEKLY, 100))
        self.assertTrue(app._is_zero_quota_payload(payload, "weekly"))
        self.assertTrue(app._is_zero_quota_payload(payload, "any"))

    def test_missing_selected_window_falls_back_to_the_other_one(self):
        """选了 5h 但账号只有周窗口：回退判定，不能静默停掉回收。"""
        payload = _payload(primary=(WEEKLY, 100))
        self.assertTrue(app._is_zero_quota_payload(payload, "five_hour"))

        healthy = _payload(primary=(WEEKLY, 30))
        self.assertFalse(app._is_zero_quota_payload(healthy, "five_hour"))

    def test_credits_exhausted_ignores_window_choice(self):
        payload = _payload(primary=(FIVE_HOUR, 0), secondary=(WEEKLY, 0), credits_balance=0)
        for kind in ("any", "five_hour", "weekly"):
            self.assertTrue(app._is_zero_quota_payload(payload, kind), kind)

    def test_error_payload_is_never_zero_quota(self):
        payload = {"error_code": 401, "updated_at": 0}
        for kind in ("any", "five_hour", "weekly"):
            self.assertFalse(app._is_zero_quota_payload(payload, kind), kind)

    def test_default_argument_keeps_historic_any_window_behaviour(self):
        payload = _payload(primary=(FIVE_HOUR, 100), secondary=(WEEKLY, 16))
        self.assertTrue(app._is_zero_quota_payload(payload))

    def test_window_seconds_tolerates_upstream_jitter(self):
        """上游偶尔给 17999/604799 之类的边界值，不该因此认不出窗口。"""
        payload = _payload(primary=(17_940, 100), secondary=(604_500, 10))
        self.assertTrue(app._is_zero_quota_payload(payload, "five_hour"))
        self.assertFalse(app._is_zero_quota_payload(payload, "weekly"))

    def test_unrecognised_window_seconds_falls_back_to_any(self):
        """窗口时长对不上已知种类时，回退到旧口径而不是当作没耗尽。"""
        payload = _payload(primary=(3600, 100))
        self.assertTrue(app._is_zero_quota_payload(payload, "five_hour"))
        self.assertTrue(app._is_zero_quota_payload(payload, "weekly"))

    def test_missing_window_seconds_falls_back_to_any(self):
        payload = {"primary": {"used_percent": 100}, "secondary": {}}
        self.assertTrue(app._is_zero_quota_payload(payload, "weekly"))


class ZeroQuotaWindowNormalisationTests(unittest.TestCase):
    def test_unknown_values_fall_back_to_weekly(self):
        """空值/非法值回落到默认口径「仅看周限制」。"""
        for value in ("", None, "hourly", "5h", 5):
            self.assertEqual(
                app._normalize_trash_zero_quota_window(value),
                "weekly",
                msg=repr(value),
            )

    def test_supported_values_survive_case_and_padding(self):
        self.assertEqual(app._normalize_trash_zero_quota_window(" Five_Hour "), "five_hour")
        self.assertEqual(app._normalize_trash_zero_quota_window("WEEKLY"), "weekly")
        self.assertEqual(app._normalize_trash_zero_quota_window("ANY "), "any")


class ZeroQuotaWindowSettingTests(unittest.TestCase):
    def _workspace(self, tmp: str) -> Path:
        path = Path(tmp) / "test.db"
        db.init_db()
        db.import_workspace_sessions(
            "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
            "----socks5://127.0.0.1:1080"
        )
        return path

    def test_setting_defaults_to_weekly_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                self._workspace(tmp)
                # 新默认口径是「仅看周限制」。
                self.assertEqual(
                    db.get_workspace_settings(1)["trash_zero_quota_window"], "weekly"
                )
                self.assertEqual(app._candidate_trash_zero_quota_window(1), "weekly")
                # 额度为 0 后延迟入箱默认 1 分钟。
                self.assertEqual(
                    db.get_workspace_settings(1)["trash_zero_delay_minutes"], 1
                )
                self.assertEqual(app._candidate_trash_delay_seconds(1), 60)
                # 显式回选 any 仍然生效，不被默认值吞掉。
                db.update_workspace_settings(1, {"trash_zero_quota_window": "any"})
                self.assertEqual(
                    db.get_workspace_settings(1)["trash_zero_quota_window"], "any"
                )
                self.assertEqual(app._candidate_trash_zero_quota_window(1), "any")

    def test_stats_expose_the_configured_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                self._workspace(tmp)
                db.update_workspace_settings(1, {"trash_zero_quota_window": "five_hour"})
                stats = db.get_workspace_candidate_stats(1)
        self.assertEqual(stats["trash"]["trash_zero_quota_window"], "five_hour")

    def test_saved_setting_survives_a_manual_quota_query(self):
        """手动查询请求体不带垃圾箱设置，模型默认值不能盖掉已保存的口径。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                self._workspace(tmp)
                db.update_workspace_settings(
                    1, {"trash_zero_quota_window": "weekly", "trash_enabled": False}
                )
                req = app.WorkspaceCandidatesReq(
                    workspace_id=1, emails=["member@example.com"]
                )
                overrides = req.model_dump()
                for key in (
                    "trash_enabled",
                    "trash_invalid_enabled",
                    "trash_zero_delay_minutes",
                    "trash_zero_quota_window",
                ):
                    if key not in req.model_fields_set:
                        overrides.pop(key, None)
                settings = app._workspace_settings_snapshot(1, overrides)

        self.assertEqual(settings["trash_zero_quota_window"], "weekly")
        self.assertFalse(settings["trash_enabled"])


if __name__ == "__main__":
    unittest.main()
