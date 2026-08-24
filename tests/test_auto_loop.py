import unittest
from unittest.mock import patch

from webui.auto_loop import AutoLoopController


class _Cursor:
    def __init__(self, statuses):
        self._statuses = iter(statuses)

    def fetchone(self):
        status, category = next(self._statuses)
        return {"status": status, "error_category": category}


class _Connection:
    def __init__(self, statuses):
        self._cursor = _Cursor(statuses)

    def execute(self, *_args):
        return self._cursor


class AutoLoopTests(unittest.TestCase):
    def test_stop_waits_for_started_run_terminal_state(self):
        controller = AutoLoopController()
        controller._stop_event.set()
        connection = _Connection([("running", None), ("done", None)])

        with patch("webui.auto_loop.db._conn", return_value=connection), \
             patch("webui.auto_loop.time.sleep", return_value=None), \
             patch("webui.auto_loop.time.time", side_effect=[0, 0, 0, 1]):
            result = controller._wait_run_finish("run-1", timeout=10)

        self.assertEqual(result, (True, ""))

    def _controller_for_stats(self):
        controller = AutoLoopController()
        controller._state = "running"
        controller._task_total = 1
        controller._task_total_known = True
        controller._account_retry_count = 1
        controller._options = {"login_only": False}
        return controller

    def test_retry_is_counted_per_account_until_final_result(self):
        controller = self._controller_for_stats()
        account = {"email": "retry@example.com", "_auto_task_key": "retry@example.com"}

        key = controller._begin_account_attempt(account)
        self.assertEqual(key, "retry@example.com")
        self.assertTrue(
            controller._finish_with_optional_retry(
                account, False, "network", pooled=False,
            )
        )
        first = controller.status()
        self.assertEqual(first["registered_ok"], 0)
        self.assertEqual(first["registered_fail"], 0)
        self.assertEqual(first["task_completed"], 0)
        self.assertEqual(first["retry_count"], 1)
        self.assertEqual(first["retry_attempts"], 1)

        # 模拟 worker 从重试队列取回同一账号。
        with controller._lock:
            retry_account = controller._registration_retry_queue.pop(0)
        controller._begin_account_attempt(retry_account)
        self.assertFalse(
            controller._finish_with_optional_retry(
                retry_account, False, "unknown", pooled=False,
            )
        )
        final = controller.status()
        self.assertEqual(final["registered_ok"], 0)
        self.assertEqual(final["registered_fail"], 1)
        self.assertEqual(final["task_completed"], 1)
        self.assertEqual(final["retry_count"], 1)

    def test_success_after_retry_counts_one_success_and_one_retry(self):
        controller = self._controller_for_stats()
        account = {"email": "ok@example.com", "_auto_task_key": "ok@example.com"}
        controller._begin_account_attempt(account)
        self.assertTrue(
            controller._finish_with_optional_retry(
                account, False, "network", pooled=False,
            )
        )
        with controller._lock:
            retry_account = controller._registration_retry_queue.pop(0)
        controller._begin_account_attempt(retry_account)
        self.assertFalse(
            controller._finish_with_optional_retry(
                retry_account, True, "", pooled=False,
            )
        )
        result = controller.status()
        self.assertEqual(result["registered_ok"], 1)
        self.assertEqual(result["registered_fail"], 0)
        self.assertEqual(result["task_completed"], 1)
        self.assertEqual(result["retry_count"], 1)


if __name__ == "__main__":
    unittest.main()
