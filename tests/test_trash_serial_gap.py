"""入箱是串行的，连续两次入箱之间的间隔可配置（默认 30 秒）。

多个候选人常常在相近时间到期，而每次入箱都要打上游的席位切换接口。这里锁住
三件事：间隔按空间设置生效、只有真正入箱的行才占用间隔（纯复查放行的不算）、
停机信号能立刻打断等待。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


class TrashGapSettingTests(unittest.TestCase):
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
                self.assertEqual(db.get_workspace_settings(1)["trash_gap_seconds"], 30)
                self.assertEqual(app._candidate_trash_gap_seconds(1), 30)

                db.update_workspace_settings(1, {"trash_gap_seconds": 120})
                self.assertEqual(app._candidate_trash_gap_seconds(1), 120)

    def test_stats_expose_the_configured_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._workspace()
                db.update_workspace_settings(1, {"trash_gap_seconds": 90})
                stats = db.get_workspace_candidate_stats(1)
        self.assertEqual(stats["trash"]["trash_gap_seconds"], 90)

    def test_zero_means_no_wait_rather_than_the_default(self):
        """0 是合法取值：显式要求不等待，不能被 ``or 30`` 还原成默认值。"""
        self.assertEqual(app._candidate_trash_gap_seconds(1, {"trash_gap_seconds": 0}), 0)

    def test_invalid_values_fall_back_and_clamp(self):
        self.assertEqual(app._candidate_trash_gap_seconds(1, {"trash_gap_seconds": "abc"}), 30)
        self.assertEqual(app._candidate_trash_gap_seconds(1, {"trash_gap_seconds": None}), 30)
        self.assertEqual(app._candidate_trash_gap_seconds(1, {"trash_gap_seconds": -5}), 0)
        self.assertEqual(app._candidate_trash_gap_seconds(1, {"trash_gap_seconds": 9999}), 600)
        self.assertEqual(app._candidate_trash_gap_seconds(1, {"trash_gap_seconds": "45"}), 45)


class _RecordingStop:
    """记录 sweeper 每次 wait 的秒数，只有被显式停止时才结束循环。"""

    def __init__(self):
        self.waits: list[float] = []
        self._stopped = False

    def is_set(self):
        return self._stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        return self._stopped

    def set(self):
        self._stopped = True


class _AbortingStop(_RecordingStop):
    """第一次被要求等待就返回 True，模拟等待期间收到停机信号。"""

    def wait(self, seconds):
        self.waits.append(seconds)
        stopped = self._stopped
        self._stopped = True
        return stopped or True


class TrashSweeperPacingTests(unittest.TestCase):
    """到期入箱路径按配置值在连续入箱之间等待。"""

    #: sweeper 每轮末尾的轮询周期；测试里用它区分"轮询等待"和"入箱间隔"。
    POLL_SECONDS = 30

    def _due_rows(self, count):
        return [
            {"workspace_master_id": 1, "email": f"m{i}@example.com"}
            for i in range(count)
        ]

    def _run_sweeper(self, *, rows, processed, gap, stop=None):
        """只跑一轮 sweeper，processed 决定每行是否算作真实入箱。

        worker 本身是 ``while not stop.is_set()`` 的常驻循环，所以让第二次取到期
        行时置停：第一轮正常处理，第二轮空转一次即退出。
        """
        stop = stop or _RecordingStop()
        settings = {"trash_gap_seconds": gap, "trash_enabled": True}
        calls = iter(processed)
        rounds = {"n": 0}

        def due_rows():
            rounds["n"] += 1
            if rounds["n"] == 1:
                return rows
            stop.set()
            return []

        with (
            patch.object(app, "_trash_sweeper_stop", stop),
            patch.object(app, "_reconcile_invalid_candidate_trash",
                         return_value={"scanned": 0, "marked": 0, "skipped": 0, "seat_pending": 0}),
            patch.object(app.db, "list_workspace_candidate_trash_due", side_effect=due_rows),
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_candidate_quota_proxy_pool", return_value=None),
            patch.object(app, "_candidate_proxy_pool_text", return_value=""),
            patch.object(app, "_process_scheduled_trash_due", side_effect=lambda *a, **k: next(calls)) as proc,
        ):
            app._trash_sweeper_worker()
        gap_waits = [w for w in stop.waits if w != self.POLL_SECONDS]
        return stop, proc, gap_waits

    def test_gap_is_applied_between_consecutive_trashes(self):
        _, proc, gap_waits = self._run_sweeper(
            rows=self._due_rows(3), processed=[True, True, True], gap=45
        )
        self.assertEqual(proc.call_count, 3)
        # 3 行入箱 → 第 2、3 行前各等一次，第 1 行不等。
        self.assertEqual(gap_waits, [45, 45])

    def test_first_trash_of_the_round_does_not_wait(self):
        _, proc, gap_waits = self._run_sweeper(rows=self._due_rows(1), processed=[True], gap=45)
        self.assertEqual(proc.call_count, 1)
        self.assertEqual(gap_waits, [])

    def test_rows_that_are_only_rechecked_do_not_consume_the_gap(self):
        """复查后额度还够、直接放行的行不该拖慢整轮。"""
        _, proc, gap_waits = self._run_sweeper(
            rows=self._due_rows(4), processed=[False, False, True, False], gap=45
        )
        self.assertEqual(proc.call_count, 4)
        # 前 3 行里只有第 3 行入箱，所以第 2、3 行都不等；只有紧跟其后的第 4 行等一次。
        self.assertEqual(gap_waits, [45])

    def test_only_the_row_right_after_a_trash_waits(self):
        """间隔跟在入箱后面，不是"本轮入过箱就一直等"。

        一轮里常见形状是开头入箱几条、后面几十条只是复查放行；若按"本轮入过箱"
        计数，那几十条会每条白等一个间隔，把整轮拖垮。
        """
        _, proc, gap_waits = self._run_sweeper(
            rows=self._due_rows(6),
            processed=[True, False, False, False, True, False],
            gap=45,
        )
        self.assertEqual(proc.call_count, 6)
        # 第 2 行（紧跟入箱）和第 6 行（紧跟入箱）各等一次，其余不等。
        self.assertEqual(gap_waits, [45, 45])

    def test_gap_of_zero_never_waits_between_rows(self):
        stop, proc, gap_waits = self._run_sweeper(
            rows=self._due_rows(3), processed=[True, True, True], gap=0
        )
        self.assertEqual(proc.call_count, 3)
        self.assertEqual(gap_waits, [])
        self.assertNotIn(0, stop.waits)

    def test_stop_signal_during_the_gap_aborts_the_round(self):
        """等待间隔时收到停机信号，不再处理剩下的到期行。"""
        stop = _AbortingStop()
        _, proc, gap_waits = self._run_sweeper(
            rows=self._due_rows(5), processed=[True] * 5, gap=60, stop=stop
        )
        # 第一行入箱后进入等待即被打断，后面 4 行不处理。
        self.assertEqual(proc.call_count, 1)
        self.assertEqual(gap_waits, [60])


class InvalidReconcilePacingTests(unittest.TestCase):
    """失效补偿入箱路径共用同一个间隔设置。"""

    def _rows(self, count):
        return [
            {
                "workspace_master_id": 1,
                "email": f"m{i}@example.com",
                "member_id": f"member-{i}",
                "workspace_join_status": "joined",
            }
            for i in range(count)
        ]

    def _run(self, *, rows, gap, stop=None):
        stop = stop or _RecordingStop()
        with (
            patch.object(app, "_trash_sweeper_stop", stop),
            patch.object(app.db, "list_invalid_workspace_candidates_pending_trash", return_value=rows),
            patch.object(app.db, "get_workspace_settings",
                         return_value={"trash_invalid_enabled": True, "trash_gap_seconds": gap}),
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app.workspace_membership, "trash_workspace_candidate",
                         return_value={"ok": True}) as trash,
        ):
            result = app._reconcile_invalid_candidate_trash()
        return stop, trash, result

    def test_gap_is_applied_between_consecutive_invalid_trashes(self):
        stop, trash, result = self._run(rows=self._rows(3), gap=30)
        self.assertEqual(result["marked"], 3)
        self.assertEqual(trash.call_count, 3)
        self.assertEqual(stop.waits, [30, 30])

    def test_skipped_rows_do_not_consume_the_gap(self):
        """没有成员关系的行会被跳过，不算入箱，不该触发等待。"""
        rows = [
            {"workspace_master_id": 1, "email": "a@example.com", "workspace_join_status": "not_invited"},
            {"workspace_master_id": 1, "email": "b@example.com", "member_id": "member-b",
             "workspace_join_status": "joined"},
        ]
        stop, trash, result = self._run(rows=rows, gap=30)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(trash.call_count, 1)
        self.assertEqual(stop.waits, [])

    def test_stop_signal_during_the_gap_aborts_reconciliation(self):
        stop = _AbortingStop()
        _, trash, _ = self._run(rows=self._rows(5), gap=60, stop=stop)
        self.assertEqual(trash.call_count, 1)
        self.assertEqual(stop.waits, [60])


class ProcessScheduledTrashReturnTests(unittest.TestCase):
    """``_process_scheduled_trash_due`` 的返回值就是限速依据，必须分得清。"""

    def _settings(self):
        return {"trash_enabled": True, "trash_invalid_enabled": True, "trash_gap_seconds": 30}

    def _row(self):
        return {"workspace_master_id": 1, "email": "m@example.com"}

    def test_returns_true_when_the_row_is_trashed(self):
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_candidate_trash_enabled", return_value=True),
            patch.object(app, "_lease_candidate_quota_proxy", return_value=""),
            patch.object(app.workspace_membership, "fetch_candidate_quota", return_value={}),
            patch.object(app, "_is_zero_quota_payload", return_value=True),
            patch.object(app, "_apply_candidate_trash", return_value={"ok": True}) as applied,
        ):
            result = app._process_scheduled_trash_due(self._row(), self._settings(), object())
        self.assertTrue(result)
        applied.assert_called_once()

    def test_returns_false_when_the_row_is_only_released(self):
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_candidate_trash_enabled", return_value=True),
            patch.object(app, "_lease_candidate_quota_proxy", return_value=""),
            patch.object(app.workspace_membership, "fetch_candidate_quota", return_value={}),
            patch.object(app, "_is_zero_quota_payload", return_value=False),
            patch.object(app, "_clear_candidate_trash_timer") as cleared,
            patch.object(app, "_apply_candidate_trash") as applied,
        ):
            result = app._process_scheduled_trash_due(self._row(), self._settings(), object())
        self.assertFalse(result)
        cleared.assert_called_once()
        applied.assert_not_called()

    def test_returns_false_when_the_quota_recheck_fails(self):
        """复查失败只是把时间后移，没打席位接口，不该占用间隔。"""
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_candidate_trash_enabled", return_value=True),
            patch.object(app, "_lease_candidate_quota_proxy", return_value=""),
            patch.object(app.workspace_membership, "fetch_candidate_quota",
                         side_effect=RuntimeError("boom")),
            patch.object(app.db, "update_workspace_candidate_trash"),
        ):
            result = app._process_scheduled_trash_due(self._row(), self._settings(), object())
        self.assertFalse(result)

    def test_returns_true_when_deactivation_triggers_invalid_trash(self):
        exc = app.workspace_membership.QuotaAccountDeactivated("deactivated")
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_candidate_trash_enabled", return_value=True),
            patch.object(app, "_lease_candidate_quota_proxy", return_value=""),
            patch.object(app.workspace_membership, "fetch_candidate_quota", side_effect=exc),
            patch.object(app, "_handle_candidate_quota_deactivated") as handled,
        ):
            result = app._process_scheduled_trash_due(self._row(), self._settings(), object())
        self.assertTrue(result)
        handled.assert_called_once()

    def test_returns_false_when_trash_is_disabled_for_the_workspace(self):
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_candidate_trash_enabled", return_value=False),
            patch.object(app, "_clear_candidate_trash_timer"),
        ):
            result = app._process_scheduled_trash_due(self._row(), self._settings(), object())
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
