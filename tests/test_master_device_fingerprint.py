"""母号会话设备指纹（oai-device-id / User-Agent）的导入与回放。

OpenAI 已把会话绑定到登录时的设备指纹：/api/auth/session 指纹不匹配时
只返回已失效会话的缓存 token（管理请求 401 token_invalidated）。
导入的 session JSON 自带 statsigContext.deviceId/userAgent，落库后在
管理请求里原样回放；老格式导入不带指纹时保留已有值。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db, workspace_membership as wm


DID = "811c9534-6ef6-4cbc-ae21-9d249f1ba805"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
SESSION_JSON = (
    '{"accessToken":"at-1","account":{"id":"ws-ext-1"},'
    '"user":{"email":"boss@example.com"},'
    '"sessionToken":"session-token-abcdefghijklmnop",'
    '"proxy":"socks5://127.0.0.1:1080",'
    '"statsigContext":{"deviceId":"%s","userAgent":"%s","ip":"1.2.3.4"}}'
    % (DID, UA)
)


def _seed_with_json(tmp, payload):
    with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
        db.init_db()
        return db.import_workspace_sessions(payload)


class ImportDeviceFingerprintTests(unittest.TestCase):
    def test_statsig_context_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = _seed_with_json(tmp, SESSION_JSON)
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self.assertEqual(res["inserted"], 1)
                m = db.list_workspace_masters()
                master = db.get_workspace_master(m[0]["id"])
                self.assertEqual(master["device_id"], DID)
                self.assertEqual(master["user_agent"], UA)
                self.assertEqual(master["workspace_id"], "ws-ext-1")

    def test_legacy_reimport_keeps_fingerprint(self):
        """带指纹导入后，再用旧的 ---- 格式重导同一会话不清空指纹。"""
        with tempfile.TemporaryDirectory() as tmp:
            _seed_with_json(tmp, SESSION_JSON)
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                res = db.import_workspace_sessions(
                    "boss@example.com----session-token-abcdefghijklmnop----socks5://p:1"
                )
                self.assertEqual(res["updated"], 1)
                master = db.get_workspace_master(db.list_workspace_masters()[0]["id"])
                self.assertEqual(master["device_id"], DID)
                self.assertEqual(master["user_agent"], UA)

    def test_top_level_device_fields_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = _seed_with_json(
                tmp,
                '{"session_token":"sess-abcdefghijklmnop","email":"a@b.com",'
                '"proxy":"socks5://127.0.0.1:1080",'
                '"device_id":"dev-top","user_agent":"Agent/1.0"}',
            )
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self.assertEqual(res["inserted"], 1)
                master = db.get_workspace_master(db.list_workspace_masters()[0]["id"])
                self.assertEqual(master["device_id"], "dev-top")
                self.assertEqual(master["user_agent"], "Agent/1.0")


class DeviceIdResolutionTests(unittest.TestCase):
    def test_prefers_stored_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_with_json(tmp, SESSION_JSON)
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self.assertEqual(wm._workspace_device_id("ws-ext-1"), DID)

    def test_fallback_uuid5_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                db.import_workspace_sessions(
                    "boss@example.com----session-token-abcdefghijklmnop----socks5://127.0.0.1:1080"
                )
                master = db.get_workspace_master(db.list_workspace_masters()[0]["id"])
                got = wm._workspace_device_id(master["workspace_id"] or "")
                # 未录指纹 → 派生 UUID 兜底，两次调用必须一致（设备要稳定）。
                self.assertEqual(got, wm._workspace_device_id(master["workspace_id"] or ""))
                self.assertNotEqual(got, DID)


class RefreshSendsFingerprintTests(unittest.TestCase):
    def test_refresh_uses_stored_device_and_ua(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_with_json(tmp, SESSION_JSON)
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                master = db.get_workspace_master(db.list_workspace_masters()[0]["id"])
                seen = {}

                class Resp:
                    status_code = 200

                    def json(self):
                        return {"accessToken": "ey.new-token"}

                class S:
                    def __init__(self):
                        self.cookies = _Cookies()

                    def get(self, url, headers=None, timeout=None):
                        seen["headers"] = headers or {}
                        return Resp()

                class _Cookies:
                    def set(self, *a, **k):
                        pass

                    def get(self, *a, **k):
                        return ""

                token = wm._refresh_workspace_access_token(master["id"], S())
                self.assertEqual(token, "ey.new-token")
                self.assertEqual(seen["headers"]["oai-device-id"], DID)
                self.assertEqual(seen["headers"]["User-Agent"], UA)


if __name__ == "__main__":
    unittest.main()
