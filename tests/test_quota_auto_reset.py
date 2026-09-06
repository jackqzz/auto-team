"""额度耗尽后自动兑换重置券（定时额度里的开关）。

券是不可逆的消耗品，所以这里锁住的重点是"什么时候**不**兑"：

* 开关默认关闭，没显式打开一张都不许兑；
* 空间额度耗尽（``workspace_member_credits_depleted``）兑了也没用，不兑；
* 限流窗口没真用尽（例如只是 credits 余额见底）券用不上，不兑；
* ``applicable`` 不是正数就不兑 —— 线上确实存在 ``available=1, applicable=0``
  的账号，只看 available 会白烧一张。

以及"兑了之后怎么办"：额度真恢复了就不入箱，兑换失败或重查失败一律照常入箱。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db, workspace_membership


FIVE_HOUR = 18000
WEEKLY = 604800


def _payload(used=100, seconds=WEEKLY, **extra):
    """构造一条 fetch_candidate_quota 落库形状的额度记录。"""
    return {
        "plan_type": "team",
        "credits_balance": None,
        "allowed": False,
        "primary": {"used_percent": used, "window_seconds": seconds, "reset_at": 0},
        "secondary": {"used_percent": None, "window_seconds": None, "reset_at": None},
        "updated_at": 0,
        **extra,
    }


def _exhausted_with_credit(applicable=1, available=1, **extra):
    return _payload(reset_credits={"available": available, "applicable": applicable}, **extra)


class AutoResetSettingTests(unittest.TestCase):
    def _workspace(self):
        db.init_db()
        db.import_workspace_sessions(
            "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
            "----socks5://127.0.0.1:1080"
        )

    def test_defaults_to_off_and_round_trips(self):
        """默认必须关闭：不能因为升级了一个版本就开始烧用户的券。"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._workspace()
                self.assertFalse(db.get_workspace_settings(1)["quota_auto_reset_enabled"])
                self.assertFalse(app._candidate_quota_auto_reset_enabled(1))

                db.update_workspace_settings(1, {"quota_auto_reset_enabled": True})
                self.assertTrue(app._candidate_quota_auto_reset_enabled(1))

    def test_schedule_request_accepts_and_defaults_the_flag(self):
        req = app.WorkspaceQuotaScheduleReq(workspace_id=1)
        self.assertFalse(req.quota_auto_reset_enabled)
        self.assertNotIn("quota_auto_reset_enabled", req.model_fields_set)

        req = app.WorkspaceQuotaScheduleReq(workspace_id=1, quota_auto_reset_enabled=True)
        self.assertTrue(req.quota_auto_reset_enabled)
        self.assertIn("quota_auto_reset_enabled", req.model_fields_set)


class AutoResetGuardTests(unittest.TestCase):
    """判断"这条额度值不值得兑券"。空字符串 = 值得试。"""

    def test_allows_an_exhausted_window_with_an_applicable_credit(self):
        self.assertEqual(app._quota_auto_reset_blocked_reason(_exhausted_with_credit()), "")

    def test_blocks_workspace_level_depletion(self):
        """空间池子被掏空是空间级问题，重置券只重置速率窗口，兑了照样是 0。"""
        payload = _exhausted_with_credit(
            rate_limit_reached_type="workspace_member_credits_depleted"
        )
        self.assertIn("空间额度耗尽", app._quota_auto_reset_blocked_reason(payload))

    def test_blocks_when_only_the_credits_balance_is_exhausted(self):
        """余额见底但限流窗口还有：券用不上。"""
        payload = _payload(used=10, credits_balance=0,
                           reset_credits={"available": 1, "applicable": 1})
        # 入箱判定认为它耗尽了……
        self.assertTrue(app._is_zero_quota_payload(payload))
        # ……但兑券救不了余额，所以不许兑。
        self.assertIn("限流窗口未耗尽", app._quota_auto_reset_blocked_reason(payload))

    def test_blocks_when_applicable_is_zero_even_with_a_credit_in_hand(self):
        """线上真实形状：available=1 但 applicable=0，兑了直接作废。"""
        payload = _exhausted_with_credit(applicable=0, available=1)
        self.assertIn("没有可用的重置券", app._quota_auto_reset_blocked_reason(payload))

    def test_blocks_when_applicable_is_missing(self):
        """字段缺失（旧记录/上游没返回）时宁可不兑，不按猜测消耗。"""
        payload = _payload(reset_credits={"available": 1})
        self.assertIn("未返回", app._quota_auto_reset_blocked_reason(payload))
        self.assertIn("未返回", app._quota_auto_reset_blocked_reason(_payload()))

    def test_blocks_an_error_payload(self):
        self.assertTrue(app._quota_auto_reset_blocked_reason({"error_code": 401, "updated_at": 0}))

    def test_honours_the_configured_window_kind(self):
        """选"仅看周限制"时，5h 用尽不该触发兑券。"""
        payload = _payload(used=100, seconds=FIVE_HOUR,
                           reset_credits={"available": 1, "applicable": 1})
        payload["secondary"] = {"used_percent": 20, "window_seconds": WEEKLY, "reset_at": 0}
        self.assertEqual(app._quota_auto_reset_blocked_reason(payload, "five_hour"), "")
        self.assertIn("限流窗口未耗尽", app._quota_auto_reset_blocked_reason(payload, "weekly"))


class AutoResetAttemptTests(unittest.TestCase):
    """``_try_candidate_quota_auto_reset``：返回非 None 才代表"额度已恢复"。"""

    SETTINGS = {"quota_auto_reset_enabled": True, "trash_zero_quota_window": "any"}

    def _run(self, payload, settings=None, **patches):
        defaults = {
            "consume_candidate_reset_credit": {"credit_id": "c1", "windows_reset": ["primary"]},
            "fetch_candidate_quota": _payload(used=0),
        }
        defaults.update(patches)

        def kw(name):
            value = defaults[name]
            return {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}

        with (
            patch.object(app, "_lease_candidate_quota_proxy", return_value="socks5://p:1080"),
            patch.object(app.workspace_membership, "consume_candidate_reset_credit",
                         **kw("consume_candidate_reset_credit")) as consume,
            patch.object(app.workspace_membership, "fetch_candidate_quota",
                         **kw("fetch_candidate_quota")),
        ):
            result = app._try_candidate_quota_auto_reset(
                1, "one@example.com", payload,
                dict(self.SETTINGS if settings is None else settings),
                quota_leases=object(), source="test",
            )
        return result, consume

    def test_disabled_setting_never_consumes(self):
        result, consume = self._run(_exhausted_with_credit(),
                                    settings={"quota_auto_reset_enabled": False})
        self.assertIsNone(result)
        consume.assert_not_called()

    def test_blocked_payload_never_consumes(self):
        result, consume = self._run(_exhausted_with_credit(applicable=0))
        self.assertIsNone(result)
        consume.assert_not_called()

    def test_consumes_once_and_returns_the_refreshed_quota(self):
        result, consume = self._run(_exhausted_with_credit())
        self.assertEqual(consume.call_count, 1)
        self.assertEqual(result["primary"]["used_percent"], 0)

    def test_consume_failure_is_swallowed_and_reports_no_recovery(self):
        """兑换失败不能打断整轮额度任务，也不能假装额度恢复了。"""
        result, consume = self._run(
            _exhausted_with_credit(),
            consume_candidate_reset_credit=RuntimeError("upstream down"),
        )
        self.assertIsNone(result)
        self.assertEqual(consume.call_count, 1)

    def test_missing_credit_upstream_is_not_an_error(self):
        """落库的券数只是快照，上游可能已经把券收走了。"""
        result, _ = self._run(
            _exhausted_with_credit(),
            consume_candidate_reset_credit=workspace_membership.ResetCreditUnavailable("没券"),
        )
        self.assertIsNone(result)

    def test_refresh_failure_reports_no_recovery(self):
        """券花掉了但没有证据说明额度回来了：按原额度继续，照常入箱。

        入箱有延迟，到期复查会再看一次额度，真恢复了自然会被放行。
        """
        result, consume = self._run(
            _exhausted_with_credit(),
            fetch_candidate_quota=RuntimeError("timeout"),
        )
        self.assertIsNone(result)
        self.assertEqual(consume.call_count, 1)


class AutoResetTrashInteractionTests(unittest.TestCase):
    """到期复查路径：兑换后额度恢复的账号不入箱，否则照常入箱。"""

    def _recheck(self, refreshed, *, quota=None):
        settings = {"quota_auto_reset_enabled": True, "trash_enabled": True}
        row = {"workspace_master_id": 1, "email": "one@example.com"}
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_candidate_trash_enabled", return_value=True),
            patch.object(app, "_lease_candidate_quota_proxy", return_value="socks5://p:1080"),
            patch.object(app, "_candidate_quota_network_retries", return_value=0),
            patch.object(app.workspace_membership, "fetch_candidate_quota",
                         return_value=quota if quota is not None else _exhausted_with_credit()),
            patch.object(app, "_try_candidate_quota_auto_reset", return_value=refreshed) as attempt,
            patch.object(app, "_apply_candidate_trash") as apply_trash,
            patch.object(app, "_clear_candidate_trash_timer") as clear_timer,
        ):
            trashed = app._process_scheduled_trash_due(row, settings, quota_leases=object())
        return trashed, attempt, apply_trash, clear_timer

    def test_recovered_quota_cancels_the_trash(self):
        trashed, attempt, apply_trash, clear_timer = self._recheck(_payload(used=0))
        self.assertFalse(trashed)
        attempt.assert_called_once()
        apply_trash.assert_not_called()
        clear_timer.assert_called_once()

    def test_failed_reset_still_trashes(self):
        trashed, attempt, apply_trash, clear_timer = self._recheck(None)
        self.assertTrue(trashed)
        attempt.assert_called_once()
        apply_trash.assert_called_once()
        clear_timer.assert_not_called()

    def test_healthy_quota_never_attempts_a_reset(self):
        """没耗尽的账号不该白打一次兑券判定。"""
        _, attempt, apply_trash, _ = self._recheck(None, quota=_payload(used=10))
        attempt.assert_not_called()
        apply_trash.assert_not_called()


class AutoResetScheduledWorkerTests(unittest.TestCase):
    """定时轮询路径：耗尽时先尝试自救，恢复了就不排队入箱。"""

    class _StopAfterWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True
            return True

    def _run(self, refreshed, *, quota=None):
        settings = {
            "proxy_pool": "proxy-one",
            "concurrency": 1,
            "trash_enabled": True,
            "relogin_on_401": False,
            "quota_auto_reset_enabled": True,
        }
        rows = [{
            "email": "one@example.com",
            "has_workspace_access_token": True,
            "account_status": "active",
            "seat_type": "default",
            "workspace_join_status": "joined",
        }]
        with (
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app.db, "list_workspace_candidate_options", return_value=rows),
            patch.object(app.workspace_membership, "fetch_candidate_quota",
                         return_value=quota if quota is not None else _exhausted_with_credit()),
            patch.object(app.proxy_usage, "record_lease"),
            patch.object(app, "_try_candidate_quota_auto_reset", return_value=refreshed) as attempt,
            patch.object(app, "_schedule_candidate_trash") as schedule,
        ):
            app._quota_worker(5, 30, self._StopAfterWait(), False,
                              settings["proxy_pool"], False, 1, 180, 1, 0)
        return attempt, schedule

    def test_recovered_quota_skips_the_trash_queue(self):
        attempt, schedule = self._run(_payload(used=0))
        attempt.assert_called_once()
        schedule.assert_not_called()

    def test_failed_reset_still_queues_the_trash(self):
        attempt, schedule = self._run(None)
        attempt.assert_called_once()
        schedule.assert_called_once()

    def test_healthy_quota_never_attempts_a_reset(self):
        attempt, schedule = self._run(None, quota=_payload(used=10))
        attempt.assert_not_called()
        schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
