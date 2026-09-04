import tempfile
import unittest
from pathlib import Path

from webui import db


class PublicReloginConfigTests(unittest.TestCase):
    def setUp(self):
        self._old_db_path = db.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "webui.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def test_bool_enabled_and_zero_retry_count_round_trip(self):
        db.save_public_relogin_config({
            "public_relogin_enabled": True,
            "proxy_pool": " socks5://127.0.0.1:7897 ",
            "concurrency": 4,
            "quota_queue_capacity": 512,
            "relogin_queue_capacity": 128,
            "retry_count": 0,
            "quota_timeout": 15,
            "login_timeout": 180,
        })

        cfg = db.get_public_relogin_config()
        self.assertEqual(cfg["enabled"], "1")
        self.assertEqual(cfg["proxy_pool"], "socks5://127.0.0.1:7897")
        self.assertEqual(cfg["concurrency"], "4")
        self.assertEqual(cfg["quota_queue_capacity"], "512")
        self.assertEqual(cfg["relogin_queue_capacity"], "128")
        self.assertEqual(cfg["retry_count"], "0")
        self.assertEqual(cfg["quota_timeout"], "15")
        self.assertEqual(cfg["login_timeout"], "180")

        db.save_public_relogin_config({"public_relogin_enabled": False})
        self.assertEqual(db.get_public_relogin_config()["enabled"], "0")

    def test_error_handling_thresholds_round_trip(self):
        cfg = db.get_public_relogin_config()
        self.assertEqual(cfg["rate_limit_retries"], "2")
        self.assertEqual(cfg["forbidden_streak"], str(db.PUBLIC_RELOGIN_403_STREAK))
        self.assertEqual(cfg["payment_dead_accounts"], str(db.PUBLIC_RELOGIN_402_DEAD_ACCOUNTS))

        db.save_public_relogin_config({
            # 0 是合法取值（完全不退避），不能被 falsy 兜底吃掉。
            "rate_limit_retries": 0,
            "forbidden_streak": 4,
            "payment_dead_accounts": 5,
        })

        cfg = db.get_public_relogin_config()
        self.assertEqual(cfg["rate_limit_retries"], "0")
        self.assertEqual(cfg["forbidden_streak"], "4")
        self.assertEqual(cfg["payment_dead_accounts"], "5")


    def test_effective_config_keeps_explicit_zero_but_defaults_blank(self):
        from webui import public_relogin

        db.save_public_relogin_config({"rate_limit_retries": 0})
        self.assertEqual(public_relogin.get_effective_config()["rate_limit_retries"], 0)

        # 存成空串（比如手工改库）时才该退回默认值 2，而不是跟着 0 一起不退避。
        db.set_setting("public_relogin_rate_limit_retries", "")
        self.assertEqual(public_relogin.get_effective_config()["rate_limit_retries"], 2)


if __name__ == "__main__":
    unittest.main()
