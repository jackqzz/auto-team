import threading
import unittest
from unittest.mock import Mock, call, patch

from webui import app as app_module


class QuotaSchedulerStartupTests(unittest.TestCase):
    def test_startup_hook_restores_quota_schedulers_even_when_sweeper_exists(self):
        alive_sweeper = Mock()
        alive_sweeper.is_alive.return_value = True

        with (
            patch.object(app_module, "_trash_sweeper_thread", alive_sweeper),
            patch.object(app_module, "_restore_quota_schedulers") as restore,
            patch.object(app_module.db, "list_workspace_masters", return_value=[]),
        ):
            app_module._start_background_sweeper()

        restore.assert_called_once_with()

    def test_restore_starts_only_enabled_workspaces(self):
        rows = [{"id": 11}, {"id": 12}, {"id": 13}]
        settings = {
            11: {"quota_enabled": True, "interval_minutes": 30},
            12: {"quota_enabled": False, "interval_minutes": 15},
            13: {"quota_enabled": True, "interval_minutes": 60},
        }

        with (
            patch.object(app_module.db, "list_workspace_masters", return_value=rows),
            patch.object(
                app_module.db,
                "get_workspace_settings",
                side_effect=lambda workspace_id: settings[workspace_id],
            ),
            patch.object(
                app_module,
                "_start_quota_scheduler",
                return_value=(Mock(), True),
            ) as start,
        ):
            restored = app_module._restore_quota_schedulers()

        self.assertEqual(restored, 2)
        self.assertEqual(
            start.call_args_list,
            [
                call(11, settings[11], source="startup"),
                call(13, settings[13], source="startup"),
            ],
        )

    def test_scheduler_start_is_idempotent_per_workspace(self):
        workspace_id = 987654
        entered = threading.Event()

        def worker(_workspace_id, _interval, stop, *_args):
            entered.set()
            stop.wait(5)

        try:
            with patch.object(app_module, "_quota_worker", new=worker):
                first, first_started = app_module._start_quota_scheduler(
                    workspace_id,
                    {"interval_minutes": 30},
                    source="test",
                )
                self.assertTrue(entered.wait(1))
                second, second_started = app_module._start_quota_scheduler(
                    workspace_id,
                    {"interval_minutes": 30},
                    source="test",
                )

            self.assertTrue(first_started)
            self.assertFalse(second_started)
            self.assertIs(first, second)
            self.assertEqual(first[1].name, f"quota-scheduler-{workspace_id}")
        finally:
            item = app_module._stop_quota_scheduler(workspace_id)
            if item:
                item[1].join(timeout=1)


if __name__ == "__main__":
    unittest.main()
