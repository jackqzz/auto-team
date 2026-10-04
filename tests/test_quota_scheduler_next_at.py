import threading
import time
import unittest
from unittest.mock import Mock, patch

from webui import app as app_module


class _StopAfterWaits:
    """模拟 threading.Event：wait 满指定次数后 is_set 变 True。"""

    def __init__(self, waits: int):
        self.remaining = waits
        self.waits_seen: list[float] = []

    def is_set(self):
        return self.remaining <= 0

    def set(self):
        self.remaining = 0

    def wait(self, seconds):
        self.waits_seen.append(seconds)
        self.remaining -= 1
        return True


class QuotaWorkerNextAtTests(unittest.TestCase):
    """额度调度器每轮等待前必须刷新 next_at，且间隔跟随最新设置。"""

    def _run_worker(self, *, settings, stop, box, batch=None, workspace_exists=True):
        with (
            patch.object(app_module, "_workspace_exists", return_value=workspace_exists),
            patch.object(app_module, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app_module, "_quota_worker_batch", new=batch or Mock()),
        ):
            app_module._quota_worker(7, 30, stop, False, "", False, 1, 180, 1, 0, box)

    def test_next_at_advances_every_iteration(self):
        stop = _StopAfterWaits(2)
        box = {"next_at": 0.0}
        batch = Mock()
        started = time.time()
        self._run_worker(settings={"interval_minutes": 5}, stop=stop, box=box, batch=batch)
        # 两轮迭代都按最新设置等待 5 分钟，各刷新一次 next_at。
        self.assertEqual(stop.waits_seen, [300.0, 300.0])
        self.assertGreaterEqual(batch.call_count, 1)
        self.assertAlmostEqual(box["next_at"] - started, 300.0, delta=5.0)

    def test_interval_follows_latest_settings_not_start_time(self):
        # 调度器以 30 分钟创建，但设置里已经改成 7 分钟：按 7 分钟等待。
        stop = _StopAfterWaits(1)
        box = {}
        self._run_worker(settings={"interval_minutes": 7}, stop=stop, box=box)
        self.assertEqual(stop.waits_seen, [420.0])
        self.assertAlmostEqual(box["next_at"] - time.time(), 420.0, delta=5.0)

    def test_missing_interval_in_settings_falls_back_to_start_value(self):
        stop = _StopAfterWaits(1)
        box = {}
        self._run_worker(settings={}, stop=stop, box=box)
        self.assertEqual(stop.waits_seen, [1800.0])

    def test_batch_exception_does_not_kill_the_loop(self):
        stop = _StopAfterWaits(2)
        box = {}
        batch = Mock(side_effect=[RuntimeError("boom"), None])
        self._run_worker(settings={"interval_minutes": 5}, stop=stop, box=box, batch=batch)
        # 第一次批次炸了，循环仍继续到第二次迭代才退出。
        self.assertEqual(batch.call_count, 2)
        self.assertEqual(len(stop.waits_seen), 2)
        self.assertGreater(box.get("next_at", 0), 0)

    def test_paused_iteration_still_updates_next_at(self):
        stop = _StopAfterWaits(1)
        box = {}
        batch = Mock()
        self._run_worker(
            settings={"interval_minutes": 5, "automation_paused": True},
            stop=stop,
            box=box,
            batch=batch,
        )
        batch.assert_not_called()
        self.assertEqual(stop.waits_seen, [300.0])
        self.assertGreater(box.get("next_at", 0), 0)

    def test_deleted_workspace_exits_without_waiting(self):
        stop = _StopAfterWaits(5)
        box = {}
        self._run_worker(settings={"interval_minutes": 5}, stop=stop, box=box, workspace_exists=False)
        self.assertEqual(stop.waits_seen, [])
        self.assertNotIn("next_at", box)


class SchedulerWaitTests(unittest.TestCase):
    def test_scheduler_wait_writes_next_at_before_waiting(self):
        stop = _StopAfterWaits(1)
        box = {}
        started = time.time()
        app_module._scheduler_wait(stop, 42, box)
        self.assertEqual(stop.waits_seen, [42])
        self.assertAlmostEqual(box["next_at"] - started, 42, delta=2.0)

    def test_scheduler_wait_tolerates_none_box_and_bad_seconds(self):
        stop = _StopAfterWaits(1)
        app_module._scheduler_wait(stop, "oops", None)
        self.assertEqual(stop.waits_seen, [0.0])


class QuotaSchedulerItemShapeTests(unittest.TestCase):
    def test_start_scheduler_stores_mutable_next_at_box(self):
        workspace_id = 987655
        entered = threading.Event()

        def worker(_workspace_id, _interval, stop, *_args):
            entered.set()
            stop.wait(5)

        try:
            with patch.object(app_module, "_quota_worker", new=worker):
                item, started = app_module._start_quota_scheduler(
                    workspace_id,
                    {"interval_minutes": 5},
                    source="test",
                )
            self.assertTrue(started)
            self.assertTrue(entered.wait(1))
            box = item[4]
            self.assertIsInstance(box, dict)
            self.assertAlmostEqual(box["next_at"] - time.time(), 300.0, delta=5.0)
        finally:
            item = app_module._stop_quota_scheduler(workspace_id)
            if item:
                item[1].join(timeout=1)


class QuotaScheduleStatusEndpointTests(unittest.TestCase):
    def test_status_returns_next_at_from_worker_box(self):
        workspace_id = 987654
        box = {"next_at": 12345.0}
        thread = Mock()
        thread.is_alive.return_value = True
        item = (threading.Event(), thread, 5, True, box)
        app_module._quota_schedulers[workspace_id] = item
        try:
            with patch.object(
                app_module.db,
                "get_workspace_settings",
                return_value={"interval_minutes": 5, "quota_enabled": True},
            ):
                payload = app_module.api_quota_schedule_status(workspace_id)
        finally:
            app_module._quota_schedulers.pop(workspace_id, None)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["running"])
        self.assertEqual(payload["next_at"], 12345.0)


class SeatWorkerNextAtTests(unittest.TestCase):
    """席位补齐 worker 的轮询等待同样刷新 next_at。"""

    def test_paused_seat_worker_updates_next_at_box(self):
        stop = _StopAfterWaits(1)
        box = {}
        settings = {"automation_paused": True, "auto_seat_interval_minutes": 5}
        with (
            patch.object(app_module, "_workspace_exists", return_value=True),
            patch.object(app_module, "_workspace_settings_snapshot", return_value=settings),
        ):
            app_module._auto_standard_seat_worker(7, stop, box)
        self.assertEqual(stop.waits_seen, [300])
        self.assertGreater(box.get("next_at", 0), 0)


if __name__ == "__main__":
    unittest.main()
