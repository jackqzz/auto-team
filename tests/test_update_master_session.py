"""单个母号直接更新 Session：POST /api/workspaces/{id}/session。

复用批量导入的解析器：行格式只换 session；含 statsigContext 的 session
JSON 同步更新设备指纹与 UA；粘贴/显式 proxy 覆盖专属代理，否则保留。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


DID = "811c9534-6ef6-4cbc-ae21-9d249f1ba805"
UA = "Agent/Chrome-154"
SESSION = "new-session-token-0123456789abcdef"


def _seed_master(tmp, email="boss@example.com", proxy="socks5://127.0.0.1:1080", wid="ws-ext-1"):
    db.init_db()
    db.import_workspace_sessions(
        json.dumps({
            "accessToken": "at-old",
            "account": {"id": wid},
            "user": {"email": email},
            "sessionToken": "old-session-abcdefghijklmnop",
            "proxy": proxy,
        })
    )
    return db.list_workspace_masters()[0]["id"]


def _req(session, proxy=None):
    return app.WorkspaceSessionUpdateReq(session=session, proxy=proxy)


class UpdateSessionEndpointTests(unittest.TestCase):
    def test_full_json_updates_session_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                wid = _seed_master(tmp)
                payload = json.dumps({
                    "accessToken": "at-new",
                    "account": {"id": "ws-ext-1"},
                    "user": {"email": "boss@example.com"},
                    "sessionToken": SESSION,
                    "statsigContext": {"deviceId": DID, "userAgent": UA},
                })
                res = app.api_update_workspace_session(wid, _req(payload))
                self.assertTrue(res["ok"])
                self.assertTrue(res["has_device_fingerprint"])
                m = db.get_workspace_master(wid)
                self.assertEqual(m["session_token"], SESSION)
                self.assertEqual(m["access_token"], "at-new")
                self.assertEqual(m["device_id"], DID)
                self.assertEqual(m["user_agent"], UA)
                self.assertEqual(m["proxy_url"], "socks5://127.0.0.1:1080")

    def test_line_format_keeps_proxy_and_fingerprint(self):
        """行格式粘贴只换 session：保留已有代理与指纹。"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                wid = _seed_master(tmp)
                db.update_workspace_master_session(
                    wid, {"session_token": "s", "device_id": DID, "user_agent": UA}
                )
                res = app.api_update_workspace_session(
                    wid, _req(f"boss@example.com----{SESSION}")
                )
                self.assertTrue(res["ok"])
                self.assertFalse(res["has_device_fingerprint"])
                m = db.get_workspace_master(wid)
                self.assertEqual(m["session_token"], SESSION)
                self.assertEqual(m["proxy_url"], "socks5://127.0.0.1:1080")
                self.assertEqual(m["device_id"], DID)  # 已有指纹不被清空

    def test_email_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                wid = _seed_master(tmp)
                payload = json.dumps({
                    "sessionToken": SESSION,
                    "user": {"email": "other@example.com"},
                })
                with self.assertRaises(app.HTTPException) as ctx:
                    app.api_update_workspace_session(wid, _req(payload))
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("不一致", ctx.exception.detail)

    def test_workspace_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                wid = _seed_master(tmp)
                payload = json.dumps({
                    "accessToken": "at",
                    "account": {"id": "ws-other"},
                    "sessionToken": SESSION,
                })
                with self.assertRaises(app.HTTPException) as ctx:
                    app.api_update_workspace_session(wid, _req(payload))
                self.assertEqual(ctx.exception.status_code, 400)

    def test_proxy_override_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                wid = _seed_master(tmp)
                app.api_update_workspace_session(
                    wid, _req(f"boss@example.com----{SESSION}",
                              proxy="socks5://10.0.0.2:9999")
                )
                self.assertEqual(
                    db.get_workspace_master(wid)["proxy_url"], "socks5://10.0.0.2:9999"
                )

    def test_404_unknown_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                with self.assertRaises(app.HTTPException) as ctx:
                    app.api_update_workspace_session(999, _req("x----" + SESSION))
                self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
