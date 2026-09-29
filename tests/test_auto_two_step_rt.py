import threading
import time
import unittest
from unittest.mock import Mock, patch

from webui.auto_loop import AutoLoopController, AutoLoopState


def _two_step_controller(**overrides):
    """构造一个已“启动”的两段式控制器，不真正拉后台线程。"""
    controller = AutoLoopController()
    controller._state = AutoLoopState.RUNNING
    controller._options = {
        "login_only": False,
        "rt_two_step": True,
        "concurrency": 1,
        # 第一段被 start() 强制改写后的形态
        "want_refresh_token": False,
        "auto_export": False,
    }
    controller._options.update(overrides.pop("options", {}))
    controller._rt_two_step = True
    controller._rt_delay_seconds = overrides.pop("delay", 60.0)
    controller._rt_auto_export = overrides.pop("auto_export", True)
    controller._mail_pooled = overrides.pop("pooled", True)
    controller._mail_source_kind = "outlook"
    controller._target_count = overrides.pop("target", 0)
    controller._task_total = 10
    controller._task_total_known = True
    return controller


class RtTwoStepGateTests(unittest.TestCase):
    """第一段收口判定 + 冷却 arming + 到期派发。"""

    def test_not_drained_while_retry_queue_or_inflight_register(self):
        c = _two_step_controller()
        with c._lock:
            c._registration_retry_queue = [{"email": "a@x.com"}]
        self.assertFalse(c._registration_drained())

        with c._lock:
            c._registration_retry_queue = []
            c._worker_status[0] = {"email": "b@x.com", "stage": "register"}
        with patch("webui.auto_loop.db.count_accounts", return_value=0):
            self.assertFalse(c._registration_drained())

        # rt 阶段的在跑登录不算“在途注册”，不挡收口
        with c._lock:
            c._worker_status[0] = {"email": "b@x.com", "stage": "rt"}
        with patch("webui.auto_loop.db.count_accounts", return_value=0):
            self.assertTrue(c._registration_drained())

    def test_pooled_drained_when_pool_empty(self):
        c = _two_step_controller()
        with patch("webui.auto_loop.db.count_accounts", return_value=3):
            self.assertFalse(c._registration_drained())
        with patch("webui.auto_loop.db.count_accounts", return_value=0):
            self.assertTrue(c._registration_drained())

    def test_pooled_drained_by_target_even_when_pool_not_empty(self):
        """执行数量限制也是批次边界：终态数达标即收口，不要求号池抽干。"""
        c = _two_step_controller(target=2)
        with c._lock:
            c._account_records = {
                "a": {"status": "rt_pending"},
                "b": {"status": "failed"},
            }
        with patch("webui.auto_loop.db.count_accounts", return_value=99):
            self.assertTrue(c._registration_drained())

    def test_nonpooled_drained_only_by_target(self):
        c = _two_step_controller(pooled=False, target=2)
        self.assertFalse(c._registration_drained())
        with c._lock:
            c._account_records = {
                "a": {"status": "rt_pending"},
                "b": {"status": "failed"},
            }
        self.assertTrue(c._registration_drained())

    def test_pop_mature_rt_waits_for_drain_then_delay(self):
        c = _two_step_controller(delay=60)
        with c._lock:
            c._rt_pending.append({"email": "a@x.com", "ready_at": time.time() - 1})

        # 池里还有号 → 未收口，即使 ready_at 已过也不派发
        with patch("webui.auto_loop.db.count_accounts", return_value=5):
            self.assertIsNone(c._pop_mature_rt())

        # 收口瞬间统一起算：ready_at 被重设到 arm+delay，旧的过期时间不生效
        with patch("webui.auto_loop.db.count_accounts", return_value=0), \
             patch("webui.auto_loop.time.time", return_value=1000.0):
            self.assertIsNone(c._pop_mature_rt())
        self.assertTrue(c._rt_delay_armed)
        self.assertAlmostEqual(c._rt_pending[0]["ready_at"], 1060.0)

        # 冷却期到 → 派发
        with patch("webui.auto_loop.time.time", return_value=1061.0):
            item = c._pop_mature_rt()
        self.assertIsNotNone(item)
        self.assertEqual(item["email"], "a@x.com")

    def test_zero_delay_dispatches_immediately_after_drain(self):
        c = _two_step_controller(delay=0)
        with c._lock:
            c._rt_pending.append({"email": "a@x.com", "ready_at": 1e18})
        with patch("webui.auto_loop.db.count_accounts", return_value=0):
            self.assertIsNotNone(c._pop_mature_rt())


class RtPendingEnqueueTests(unittest.TestCase):
    """第一段成功后进入冷却队列的字段与状态。"""

    def test_enqueue_builds_login_fields_and_marks_record(self):
        c = _two_step_controller()
        with c._lock:
            c._account_records["a@x.com"] = {
                "email": "a@x.com", "status": "running",
                "attempts": 1, "retry_count": 0,
            }
        account = {"email": "a@x.com", "kind": "outlook", "group_name": "g1"}
        with patch("webui.auto_loop.db.get_registered", return_value={
            "password": "pw123", "totp_secret": "TOTPSEC",
            "mail_kind": "outlook", "group_name": "g1",
        }), patch("webui.auto_loop.db.get_account", return_value={
            "kind": "outlook", "relay_url": "",
        }):
            c._enqueue_rt_pending("a@x.com", account, "run-1")

        rec = c._account_records["a@x.com"]
        self.assertEqual(rec["status"], "rt_pending")
        self.assertEqual(len(c._rt_pending), 1)
        item = c._rt_pending[0]
        self.assertTrue(item["_rt_step2"])
        self.assertEqual(item["login_password"], "pw123")
        self.assertEqual(item["totp_secret"], "TOTPSEC")
        self.assertEqual(item["_auto_task_key"], "a@x.com")
        # 未收口时 ready_at 先按入队时间起算占位，arm 时会被统一重设
        self.assertGreater(item["ready_at"], time.time())

    def test_step1_success_does_not_count_final_result(self):
        """第一段成功不计入 task_completed/registered_ok——两段都成才算。"""
        c = _two_step_controller()
        account = {"email": "a@x.com", "_auto_task_key": "a@x.com"}
        c._begin_account_attempt(account)
        with patch("webui.auto_loop.db.get_registered", return_value={}), \
             patch("webui.auto_loop.db.get_account", return_value={}):
            c._enqueue_rt_pending("a@x.com", account, "run-1")
        snap = c.status()
        self.assertEqual(snap["registered_ok"], 0)
        self.assertEqual(snap["task_completed"], 0)
        self.assertEqual(snap["rt_pending"], 1)
        self.assertTrue(snap["rt_two_step"])

    def test_placeholder_email_resolved_from_runs_table(self):
        """非池化占位账号通过 run_id 从 runs 表取真实邮箱。"""
        c = _two_step_controller()
        account = {
            "email": "cf_placeholder_1@placeholder.local",
            "_auto_task_key": "placeholder:k:0",
        }
        with c._lock:
            c._account_records["placeholder:k:0"] = {
                "email": "cf_placeholder_1@placeholder.local",
                "status": "running", "attempts": 1, "retry_count": 0,
            }

        class _Cur:
            def fetchone(self):
                return {"email": "real@mail.com"}

        class _Con:
            def execute(self, *_a):
                return _Cur()
            def close(self):
                pass

        with patch("webui.auto_loop.db._conn", return_value=_Con()), \
             patch("webui.auto_loop.db.get_registered", return_value={
                 "password": "pw", "totp_secret": "TS", "mail_kind": "cf",
             }), \
             patch("webui.auto_loop.db.get_account", return_value={}):
            c._enqueue_rt_pending("placeholder:k:0", account, "run-x")
        self.assertEqual(c._rt_pending[0]["email"], "real@mail.com")


class RtRetryTests(unittest.TestCase):
    def test_step2_failure_requeues_to_rt_queue_not_registration(self):
        c = _two_step_controller()
        c._account_retry_count = 1
        account = {
            "email": "a@x.com", "_auto_task_key": "a@x.com", "_rt_step2": True,
        }
        c._begin_account_attempt(account)
        queued = c._finish_with_optional_retry(account, False, "network", pooled=False)
        self.assertTrue(queued)
        self.assertEqual(len(c._rt_pending), 1)
        self.assertEqual(len(c._registration_retry_queue), 0)
        # 重试不再等冷却
        self.assertLessEqual(c._rt_pending[0]["ready_at"], time.time())
        self.assertEqual(
            c._account_records["a@x.com"]["status"], "retrying",
        )

    def test_step2_success_counts_final_success(self):
        c = _two_step_controller()
        account = {
            "email": "a@x.com", "_auto_task_key": "a@x.com", "_rt_step2": True,
        }
        c._begin_account_attempt(account)
        c._finish_with_optional_retry(account, True, "", pooled=False)
        snap = c.status()
        self.assertEqual(snap["registered_ok"], 1)
        self.assertEqual(snap["task_completed"], 1)


class StartOptionsTests(unittest.TestCase):
    """start() 的两段式选项归一化。"""

    def _patch_start_env(self, pooled=True):
        provider_cls = Mock()
        provider_cls.pooled = pooled
        return [
            patch("webui.auto_loop.db.get_setting", return_value="outlook"),
            patch("webui.auto_loop.get_provider_class", return_value=provider_cls),
            patch("webui.auto_loop.db.count_accounts", return_value=5),
            patch("webui.auto_loop.db.stats", return_value={}),
            patch.object(AutoLoopController, "_manage_loop", Mock()),
        ]

    def test_rt_two_step_strips_rt_and_export_from_stage1(self):
        c = AutoLoopController()
        patches = self._patch_start_env()
        try:
            for p in patches:
                p.start()
            res = c.start({
                "rt_two_step": True,
                "rt_step_delay_minutes": 45,
                "want_refresh_token": True,
                "auto_export": True,
                "concurrency": 1,
            })
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(c._rt_two_step)
        self.assertEqual(c._rt_delay_seconds, 2700.0)
        # 第一段强制关闭 RT 与推送，第二段由 _rt_auto_export 恢复
        self.assertFalse(c._options["want_refresh_token"])
        self.assertFalse(c._options["auto_export"])
        self.assertTrue(c._rt_auto_export)
        c._stop_event.set()

    def test_rt_two_step_ignored_for_login_only(self):
        c = AutoLoopController()
        with patch("webui.auto_loop.db.list_login_candidates", return_value=[
            {"email": "a@x.com", "login_password": "pw"},
        ]), patch("webui.auto_loop.db.stats", return_value={}), \
             patch.object(AutoLoopController, "_manage_loop", Mock()):
            res = c.start({"login_only": True, "rt_two_step": True})
        self.assertTrue(res.get("ok"), res)
        self.assertFalse(c._rt_two_step)
        c._stop_event.set()

    def test_nonpooled_two_step_requires_target(self):
        c = AutoLoopController()
        patches = self._patch_start_env(pooled=False)
        try:
            for p in patches:
                p.start()
            res = c.start({"rt_two_step": True})
        finally:
            for p in patches:
                p.stop()
        self.assertFalse(res["ok"])
        self.assertIn("执行数量限制", res["error"])


class WorkerIntegrationTests(unittest.TestCase):
    """端到端走一遍 _worker_loop：2 个池号 → 第一段 → 冷却 → 第二段。"""

    def _run_worker(self, *, step1_results, delay=0.0):
        """step1_results: {email: (ok, category)}。返回 (controller, started_runs)。"""
        c = _two_step_controller(delay=delay)
        started = []  # (account_email, run_options)
        run_outcomes = {}

        def fake_start(account, options):
            run_id = f"run-{len(started)}"
            started.append((account.get("email"), dict(options)))
            run_outcomes[run_id] = account
            return run_id

        def fake_wait(run_id, timeout=1800):
            account = run_outcomes[run_id]
            if account.get("_rt_step2"):
                return True, ""
            ok, cat = step1_results.get(account.get("email"), (True, ""))
            return ok, cat

        pool = iter(step1_results.keys())
        remaining = {"n": len(step1_results)}

        def fake_claim(**_kw):
            try:
                email = next(pool)
            except StopIteration:
                return None
            remaining["n"] -= 1
            return {"email": email, "kind": "outlook"}

        def fake_count(**_kw):
            return remaining["n"]

        patches = [
            patch("webui.auto_loop.registrar.start_registration", side_effect=fake_start),
            patch.object(AutoLoopController, "_wait_run_finish", side_effect=fake_wait),
            patch("webui.auto_loop.db.get_setting", return_value="outlook"),
            patch("webui.auto_loop.db.claim_next", side_effect=fake_claim),
            patch("webui.auto_loop.db.count_accounts", side_effect=fake_count),
            patch("webui.auto_loop.db.get_registered", return_value={
                "password": "pw", "totp_secret": "TS", "mail_kind": "outlook",
            }),
            patch("webui.auto_loop.db.get_account", return_value={}),
            patch("webui.auto_loop.db.stats", return_value={}),
            patch("webui.auto_loop.db.mark_registered_permanently_invalid"),
            patch("webui.auto_loop.db.release_unused"),
            patch("webui.auto_loop.db.mark_failed"),
            patch("webui.auto_loop.time.sleep", return_value=None),
            patch("webui.auto_loop.db.is_proxy_in_cooldown", return_value=False),
        ]
        for p in patches:
            p.start()
        try:
            provider_cls = Mock()
            provider_cls.pooled = True
            with patch("webui.auto_loop.get_provider_class", return_value=provider_cls):
                c._worker_loop(0)
        finally:
            for p in patches:
                p.stop()
        return c, started

    def test_full_cycle_registers_then_rts_only_successes(self):
        c, started = self._run_worker(step1_results={
            "ok1@x.com": (True, ""),
            "bad@x.com": (False, "account"),
            "ok2@x.com": (True, ""),
        })
        emails = [e for e, _ in started]
        # 前 3 次是第一段注册（含失败的 bad），之后只有 2 个成功号进第二段
        self.assertEqual(emails[:3], ["ok1@x.com", "bad@x.com", "ok2@x.com"])
        self.assertEqual(emails[3:], ["ok1@x.com", "ok2@x.com"])
        snap = c.status()
        self.assertEqual(snap["registered_ok"], 2)
        self.assertEqual(snap["registered_fail"], 1)
        self.assertEqual(snap["task_completed"], 3)
        self.assertEqual(snap["rt_pending"], 0)

    def test_step2_run_options_use_login_semantics(self):
        c, started = self._run_worker(step1_results={"a@x.com": (True, "")})
        self.assertEqual(len(started), 2)
        _, step2_options = started[1]
        self.assertTrue(step2_options["login_only"])
        self.assertTrue(step2_options["want_refresh_token"])
        self.assertFalse(step2_options["ensure_credentials"])
        self.assertTrue(step2_options["auto_export"])  # 恢复用户原值
        # 第一段的 run_options 里 RT/推送是被剥掉的
        _, step1_options = started[0]
        self.assertFalse(step1_options["want_refresh_token"])
        self.assertFalse(step1_options["auto_export"])

    def test_cooling_blocks_dispatch_until_matured(self):
        """冷却未到点时 worker 空转不派发；手动把 ready_at 拉到过去后再跑。"""
        c = _two_step_controller(delay=3600)
        c._options["cool_down_seconds"] = 0
        c._account_retry_count = 0
        # 预置一个入队项 + 已 arm 的远未来 ready_at
        with c._lock:
            c._rt_pending.append({
                "email": "a@x.com", "_rt_step2": True,
                "ready_at": time.time() + 3600,
                "login_password": "pw", "totp_secret": "TS",
            })
            c._rt_delay_armed = True
            c._rt_armed_at = time.time()
            c._account_records["a@x.com"] = {
                "email": "a@x.com", "status": "rt_pending",
                "attempts": 1, "retry_count": 0,
            }
        started = []
        with patch("webui.auto_loop.registrar.start_registration",
                   side_effect=lambda *a: started.append(a) or "run-x"), \
             patch("webui.auto_loop.db.get_setting", return_value="outlook"), \
             patch("webui.auto_loop.db.claim_next", return_value=None), \
             patch("webui.auto_loop.db.count_accounts", return_value=0), \
             patch("webui.auto_loop.db.stats", return_value={}), \
             patch("webui.auto_loop.time.sleep", return_value=None), \
             patch("webui.auto_loop.get_provider_class") as gpc:
            gpc.return_value.pooled = True
            # 停一会让 worker 空转；冷却未到点 → 不该有 run 启动。
            # worker 不会退出（rt_pending 非空），直接走 30s 空闲上限；
            # sleep 被 mock 成瞬返，空转会很快 → 让它转固定轮数后从外部停。
            t = threading.Thread(target=c._worker_loop, args=(0,), daemon=True)
            t.start()
            for _ in range(200):
                if started:
                    break
                time.sleep(0.005)
            self.assertEqual(started, [])
            self.assertTrue(t.is_alive())  # 冷却期间不退出
            c._stop_event.set()
            t.join(timeout=5)
        self.assertEqual(started, [])


if __name__ == "__main__":
    unittest.main()
