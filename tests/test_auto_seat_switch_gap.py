"""席位补齐是串行的，两次成员切换之间的间隔可配置（默认 30 秒）。

补齐无论缺多少席位都是一轮切一个（每次重新拉一次上游席位数），原来这个间隔
是写死的 5 秒。间隔太短会让上游席位接口在一轮里被密集打，所以改成设置项。
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


class SwitchGapSettingTests(unittest.TestCase):
    def _workspace(self):
        db.init_db()
        db.import_workspace_sessions(
            "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
            "----socks5://127.0.0.1:1080"
        )

    def test_defaults_to_thirty_seconds_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._workspace()
                settings = db.get_workspace_settings(1)
                self.assertEqual(settings["auto_seat_switch_gap_seconds"], 30)
                self.assertEqual(app._auto_seat_switch_gap_seconds(settings), 30)

                db.update_workspace_settings(1, {"auto_seat_switch_gap_seconds": 90})
                self.assertEqual(
                    app._auto_seat_switch_gap_seconds(db.get_workspace_settings(1)), 90
                )

    def test_stats_expose_the_configured_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._workspace()
                db.update_workspace_settings(1, {"auto_seat_switch_gap_seconds": 45})
                stats = db.get_workspace_candidate_stats(1)
        self.assertEqual(stats["seat_fulfillment"]["auto_switch_gap_seconds"], 45)


class SwitchGapNormalisationTests(unittest.TestCase):
    def test_missing_or_invalid_values_fall_back_to_default(self):
        for settings in (None, {}, {"auto_seat_switch_gap_seconds": None},
                         {"auto_seat_switch_gap_seconds": ""},
                         {"auto_seat_switch_gap_seconds": "abc"}):
            self.assertEqual(app._auto_seat_switch_gap_seconds(settings), 30, msg=repr(settings))

    def test_zero_means_no_wait_rather_than_the_default(self):
        """0 是合法取值：显式要求不等待，不能被 ``or 30`` 吃成默认值。"""
        self.assertEqual(app._auto_seat_switch_gap_seconds({"auto_seat_switch_gap_seconds": 0}), 0)

    def test_values_are_clamped_to_the_supported_range(self):
        self.assertEqual(app._auto_seat_switch_gap_seconds({"auto_seat_switch_gap_seconds": -5}), 0)
        self.assertEqual(app._auto_seat_switch_gap_seconds({"auto_seat_switch_gap_seconds": 9999}), 600)
        self.assertEqual(app._auto_seat_switch_gap_seconds({"auto_seat_switch_gap_seconds": "120"}), 120)


class _RecordingStop:
    """记录 worker 每次 wait 的秒数，并在若干次切换后收工。"""

    def __init__(self, max_switches: int):
        self.waits: list[float] = []
        self.max_switches = max_switches
        self.switches = 0
        self._stopped = False

    def is_set(self):
        return self._stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.switches >= self.max_switches:
            self._stopped = True
        return self._stopped

    def set(self):
        self._stopped = True


class SwitchGapWorkerTests(unittest.TestCase):
    """worker 真的按配置值等待，而不是原来写死的 5 秒。"""

    def _run_standard(self, gap, switches=2):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": gap,
        }
        stop = _RecordingStop(switches)
        seat_info = {"seats_default_entitled": 10, "seats_default": 0}

        def switch(*_args, **_kwargs):
            stop.switches += 1
            return {"ok": True}

        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            patch.object(
                app,
                "_workspace_auto_standard_candidates",
                side_effect=lambda _ws, attempted: [
                    {"email": f"m{len(attempted)}@example.com"}
                ],
            ),
            patch.object(app, "_switch_candidate_to_default_and_verify", side_effect=switch),
            patch.object(app, "_enqueue_workspace_credentials"),
        ):
            app._auto_standard_seat_worker(1, stop)
        return stop

    def _run_prolite(self, gap, switches=2):
        settings = {
            "auto_prolite_seat_enabled": True,
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": gap,
            "auto_prolite_candidate_seat_type": "default",
        }
        stop = _RecordingStop(switches)
        seat_info = {"seats_prolite_entitled": 10, "seats_prolite": 0}

        def switch(*_args, **_kwargs):
            stop.switches += 1
            return {"ok": True}

        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_prolite_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            patch.object(
                app,
                "_workspace_auto_prolite_candidates",
                side_effect=lambda _ws, attempted, _kind: [
                    {"email": f"m{len(attempted)}@example.com"}
                ],
            ),
            patch.object(app, "_switch_candidate_to_prolite_and_verify", side_effect=switch),
            patch.object(app, "_enqueue_workspace_credentials"),
        ):
            app._auto_prolite_seat_worker(1, stop)
        return stop

    def test_standard_worker_waits_the_configured_gap_between_switches(self):
        stop = self._run_standard(30)
        self.assertGreaterEqual(stop.switches, 2)
        # 每次切换后的等待都是配置值，而不是历史写死的 5 秒。
        self.assertIn(30, stop.waits)
        self.assertNotIn(5, stop.waits)

    def test_prolite_worker_waits_the_configured_gap_between_switches(self):
        stop = self._run_prolite(45)
        self.assertGreaterEqual(stop.switches, 2)
        self.assertIn(45, stop.waits)
        self.assertNotIn(5, stop.waits)

    def test_switching_stays_serial_regardless_of_the_deficit(self):
        """缺 10 个席位也只是一次切一个：切换次数等于循环轮数，不会成批下发。"""
        stop = self._run_standard(0, switches=3)
        # 每轮切一个候选，切换数与轮数一致（间隔 0 时不额外等待，wait 仍每轮调用一次）。
        self.assertEqual(stop.switches, 3)
        self.assertEqual([w for w in stop.waits if w == 0], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
