import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db, proxy_usage
from webui.auto_loop import AutoLoopController, AutoLoopState
from webui.public_relogin import ProxyLeasePool


class ProxyUsagePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path_patch = patch.object(
            db,
            "DB_PATH",
            Path(self.temp_dir.name) / "proxy-usage.db",
        )
        self.db_path_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_snapshot_aggregates_task_types_and_each_proxy(self):
        proxy_usage.record_lease("proxy-one", "register", "auto_register")
        proxy_usage.record_lease("proxy-one", "login", "public_401_relogin")
        proxy_usage.record_lease("proxy-two", "quota", "public_quota")
        proxy_usage.record_lease("proxy-two", "quota", "public_quota")
        proxy_usage.record_lease("proxy-three", "candidate_join", "candidate_join")

        result = proxy_usage.snapshot()
        categories = {
            item["task_type"]: item["leased_count"]
            for item in result["categories"]
        }
        per_proxy = {item["proxy"]: item for item in result["proxies"]}

        self.assertTrue(result["persistent"])
        self.assertEqual(result["leased_count"], 5)
        self.assertEqual(categories["register"], 1)
        self.assertEqual(categories["login"], 1)
        self.assertEqual(categories["quota"], 2)
        self.assertEqual(categories["candidate_join"], 1)
        self.assertEqual(per_proxy["proxy-one"]["leased_count"], 2)
        self.assertEqual(per_proxy["proxy-two"]["quota"], 2)

    def test_reset_clears_only_usage_and_starts_new_period(self):
        proxy_usage.record_lease("proxy-one", "register", "auto_register")
        before = proxy_usage.snapshot()["started_at"]

        result = proxy_usage.reset()

        self.assertEqual(result["leased_count"], 0)
        self.assertEqual(result["proxies"], [])
        self.assertGreaterEqual(result["started_at"], before)


class ProxyUsageHookTests(unittest.TestCase):
    @patch("webui.public_relogin.proxy_usage.record_lease")
    def test_public_pool_records_explicit_task_category(self, record_lease):
        pool = ProxyLeasePool(["proxy-one"])

        pool.lease(task_type="quota", task_detail="public_quota")

        record_lease.assert_called_once_with(
            "proxy-one",
            "quota",
            "public_quota",
        )

    @patch("webui.auto_loop.proxy_usage.record_lease")
    def test_auto_loop_records_pool_lease_but_not_fixed_proxy(self, record_lease):
        controller = AutoLoopController()
        controller._state = AutoLoopState.RUNNING
        controller._options = {"login_only": True, "proxy": "fixed-proxy"}
        controller._proxy_pool = ["pool-proxy"]
        controller._proxy_usage = [0]

        selected = controller._proxy_for_worker(
            task_detail="workspace_credentials",
        )

        self.assertEqual(selected, "pool-proxy")
        record_lease.assert_called_once_with(
            "pool-proxy",
            "login",
            "workspace_credentials",
        )


if __name__ == "__main__":
    unittest.main()
