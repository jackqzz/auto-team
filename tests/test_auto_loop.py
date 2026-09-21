import threading
import unittest
from unittest.mock import Mock, patch

from webui.auto_loop import (
    AutoLoopController,
    AutoLoopState,
    _login_context_key,
    login_controller_for,
)
from webui.registrar import classify_error


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
    def test_login_context_separates_credential_ensuring_strategy(self):
        base = {"workspace_db_id": 7, "login_only": True}
        self.assertEqual(
            _login_context_key({**base, "ensure_credentials": True}),
            "workspace:7:ensure",
        )
        self.assertEqual(
            _login_context_key({**base, "ensure_credentials": False}),
            "workspace:7:refresh",
        )
        self.assertNotEqual(
            _login_context_key({**base, "ensure_credentials": True}),
            _login_context_key({**base, "ensure_credentials": False}),
        )

    def test_login_controllers_are_not_shared_between_strategies(self):
        ensured = login_controller_for(
            workspace_db_id="test-isolation",
            ensure_credentials=True,
        )
        refreshed = login_controller_for(
            workspace_db_id="test-isolation",
            ensure_credentials=False,
        )
        self.assertIsNot(ensured, refreshed)

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

    def test_missing_totp_secret_is_not_retried_or_marked_as_account_invalid(self):
        controller = self._controller_for_stats()
        account = {"email": "no-secret@example.com", "_auto_task_key": "no-secret@example.com"}
        key = controller._begin_account_attempt(account)
        self.assertEqual(
            classify_error(
                "账号已启用 2FA，但本地没有 totp_secret，无法完成登录；"
                "请从原始备份导入 2FA secret"
            ),
            "credential",
        )
        self.assertFalse(
            controller._finish_with_optional_retry(
                account, False, "credential", pooled=False,
            )
        )
        result = controller.status()
        self.assertEqual(result["registered_fail"], 1)
        self.assertEqual(result["retry_count"], 0)

    def test_network_break_threshold_scales_with_worker_count(self):
        controller = AutoLoopController()
        # start() parses the configured concurrency before launching workers;
        # use the same bounds here without starting a real background task.
        controller._options = {"concurrency": 12}
        controller._concurrency = max(
            1, min(20, int(controller._options.get("concurrency") or 1))
        )
        controller._circuit_break_threshold = max(3, 3 * controller._concurrency)
        self.assertEqual(controller._circuit_break_threshold, 36)
        controller._options = {"concurrency": 1}
        controller._concurrency = 1
        controller._circuit_break_threshold = max(3, 3 * controller._concurrency)
        self.assertEqual(controller._circuit_break_threshold, 3)

    def test_login_worker_waits_before_exiting_on_empty_queue(self):
        """登录队列瞬时不空时 worker 不能立即退出——重试/追加的账号
        可能还在别的 worker 手里跑，立即退出会让并发永久塌缩。"""
        controller = AutoLoopController()
        controller._state = AutoLoopState.RUNNING
        controller._options = {"login_only": True}
        controller._login_queue = []
        controller._target_count = 0

        with patch("webui.auto_loop.time.sleep") as sleep_mock:
            controller._worker_loop(0)

        # 宽限期约 30s（10 轮 × 30 次 0.1s 轮询），瞬间退出时 sleep 一次都不会调
        self.assertGreaterEqual(sleep_mock.call_count, 30)

    def test_respawn_workers_fills_alive_deficit(self):
        """队列补充新候选后，已退出的 worker 按 concurrency 缺口补拉。"""
        controller = AutoLoopController()
        controller._state = AutoLoopState.RUNNING
        controller._concurrency = 3
        controller._next_worker_id = 3
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        stop = threading.Event()
        alive = threading.Thread(target=stop.wait, daemon=True)
        alive.start()
        controller._workers = [dead, alive]
        controller._worker_loop = Mock()
        try:
            spawned = controller._respawn_workers_locked()
        finally:
            stop.set()
            alive.join(timeout=1)

        self.assertEqual(spawned, 2)
        self.assertEqual(len(controller._workers), 4)
        for _ in range(50):
            if controller._worker_loop.call_count >= 2:
                break
            threading.Event().wait(0.01)
        self.assertEqual(
            sorted(c.args[0] for c in controller._worker_loop.call_args_list),
            [3, 4],
        )

    def test_respawn_skips_when_workers_at_capacity(self):
        """存活 worker 已达并发上限时不重复补拉。"""
        controller = AutoLoopController()
        controller._state = AutoLoopState.RUNNING
        controller._concurrency = 1
        controller._next_worker_id = 1
        stop = threading.Event()
        alive = threading.Thread(target=stop.wait, daemon=True)
        alive.start()
        controller._workers = [alive]
        controller._worker_loop = Mock()
        try:
            self.assertEqual(controller._respawn_workers_locked(), 0)
            self.assertEqual(len(controller._workers), 1)
        finally:
            stop.set()
            alive.join(timeout=1)

    def test_respawn_skips_before_initial_spawn(self):
        """manage_loop 初始 spawn 未收尾（_workers 为空）时不补拉。"""
        controller = AutoLoopController()
        controller._state = AutoLoopState.RUNNING
        controller._concurrency = 5
        controller._workers = []
        controller._worker_loop = Mock()

        self.assertEqual(controller._respawn_workers_locked(), 0)
        controller._worker_loop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
