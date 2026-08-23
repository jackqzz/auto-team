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
            "workspace_whitelist": " workspace-1 \n workspace-2 ",
            "proxy_pool": " socks5://127.0.0.1:7897 ",
            "concurrency": 4,
            "retry_count": 0,
            "quota_timeout": 15,
            "login_timeout": 180,
        })

        cfg = db.get_public_relogin_config()
        self.assertEqual(cfg["enabled"], "1")
        self.assertEqual(cfg["workspace_whitelist"], "workspace-1 \n workspace-2")
        self.assertEqual(cfg["proxy_pool"], "socks5://127.0.0.1:7897")
        self.assertEqual(cfg["concurrency"], "4")
        self.assertEqual(cfg["retry_count"], "0")
        self.assertEqual(cfg["quota_timeout"], "15")
        self.assertEqual(cfg["login_timeout"], "180")

        db.save_public_relogin_config({"public_relogin_enabled": False})
        self.assertEqual(db.get_public_relogin_config()["enabled"], "0")


if __name__ == "__main__":
    unittest.main()
