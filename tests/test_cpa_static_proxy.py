import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db, exporter, public_relogin, registrar, workspace_membership


def _setup_db(tmp_path: Path) -> None:
    with patch.object(db, "DB_PATH", tmp_path):
        db.init_db()
        db.import_workspace_sessions(
            "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
            "----socks5://127.0.0.1:1080"
        )


POOL = "socks5://home-a:1080\nsocks5://home-b:1080\nsocks5://home-c:1080\n"


class CpaProxyLeaseTests(unittest.TestCase):
    def test_lease_picks_least_used_and_is_sticky(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)

                first = db.lease_cpa_proxy(1, "a@example.com", POOL)
                second = db.lease_cpa_proxy(1, "b@example.com", POOL)
                third = db.lease_cpa_proxy(1, "c@example.com", POOL)
                # 三条池、三个租客 → 每个代理恰好被占一次。
                self.assertEqual(
                    sorted([first, second, third]),
                    ["socks5://home-a:1080", "socks5://home-b:1080", "socks5://home-c:1080"],
                )
                self.assertEqual(
                    db.cpa_proxy_lease_counts(1),
                    {"socks5://home-a:1080": 1, "socks5://home-b:1080": 1, "socks5://home-c:1080": 1},
                )

                # 重复租用复用原绑定，计数不变。
                self.assertEqual(db.lease_cpa_proxy(1, "a@example.com", POOL), first)
                self.assertEqual(sum(db.cpa_proxy_lease_counts(1).values()), 3)

    def test_release_decrements_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                leased = db.lease_cpa_proxy(1, "a@example.com", POOL)
                self.assertEqual(db.cpa_proxy_lease_counts(1)[leased], 1)

                released = db.release_cpa_proxy(1, "a@example.com")
                self.assertEqual(released, leased)
                self.assertEqual(db.cpa_proxy_lease_counts(1), {})
                # 再次释放是空操作，不会把计数减成负数。
                self.assertEqual(db.release_cpa_proxy(1, "a@example.com"), "")

    def test_lease_reallocates_when_bound_proxy_left_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                leased = db.lease_cpa_proxy(1, "a@example.com", "socks5://gone:1080")
                self.assertEqual(leased, "socks5://gone:1080")
                counts = db.cpa_proxy_lease_counts(1)
                self.assertEqual(counts, {"socks5://gone:1080": 1})

                # 池里没有了原绑定 → 释放旧绑定、从当前池重新分配。
                leased = db.lease_cpa_proxy(1, "a@example.com", "socks5://stay:1080\nsocks5://new:1080")
                self.assertIn(leased, {"socks5://stay:1080", "socks5://new:1080"})
                counts = db.cpa_proxy_lease_counts(1)
                self.assertNotIn("socks5://gone:1080", counts)
                self.assertEqual(sum(counts.values()), 1)


class _FakeMime:
    def __init__(self):
        self.parts = {}

    def addpart(self, name=None, data=None, filename=None, content_type=None):
        self.parts[name] = data

    def close(self):
        pass


class _FakeResp:
    def __init__(self, status_code: int, text: str = "{}"):
        self.status_code = status_code
        self.text = text

    def json(self):
        try:
            return json.loads(self.text)
        except Exception:
            return {}


class _FakeCffi:
    def __init__(self, delete_status: int = 200):
        self.captured_mime = None
        self.delete_calls = []
        self._delete_status = delete_status

    def post(self, url, multipart=None, headers=None, **kwargs):
        self.captured_mime = multipart
        return _FakeResp(200, '{"status":"ok"}')

    def delete(self, url, headers=None, **kwargs):
        self.delete_calls.append(url)
        return _FakeResp(self._delete_status)


class CpaPushProxyInjectionTests(unittest.TestCase):
    def test_export_writes_credential_proxy_url(self):
        cred = {"email": "a@example.com", "access_token": "at.token", "refresh_token": "rt"}
        cfg = {
            "cpa_url": "http://cpa.local",
            "cpa_mgmt_key": "key",
            "credential_proxy_url": "socks5://home-a:1080",
        }
        fake = _FakeCffi()
        with (
            patch.object(exporter, "_import_cffi", return_value=fake),
            patch.object(exporter, "_import_cffi_mime", return_value=_FakeMime),
        ):
            result = exporter.export_to_cpa(cred, cfg)

        self.assertTrue(result["ok"])
        uploaded = json.loads(fake.captured_mime.parts["file"].decode("utf-8"))
        self.assertEqual(uploaded["proxy_url"], "socks5://home-a:1080")

    def test_export_without_pool_writes_no_proxy_url(self):
        cred = {"email": "a@example.com", "access_token": "at.token"}
        cfg = {"cpa_url": "http://cpa.local", "cpa_mgmt_key": "key"}
        fake = _FakeCffi()
        with (
            patch.object(exporter, "_import_cffi", return_value=fake),
            patch.object(exporter, "_import_cffi_mime", return_value=_FakeMime),
        ):
            result = exporter.export_to_cpa(cred, cfg)

        self.assertTrue(result["ok"])
        uploaded = json.loads(fake.captured_mime.parts["file"].decode("utf-8"))
        self.assertNotIn("proxy_url", uploaded)

    def test_delete_treats_404_as_success(self):
        fake = _FakeCffi(delete_status=404)
        with patch.object(exporter, "_import_cffi", return_value=fake):
            result = exporter.delete_cpa_auth_file(
                {"cpa_url": "http://cpa.local", "cpa_mgmt_key": "key"}, "a@example.com"
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["not_found"])
        self.assertIn("name=a%40example.com.json", fake.delete_calls[0])


class TrashCpaCleanupTests(unittest.TestCase):
    def _trash(self, email: str, reason: str):
        """跑真实的 trash_workspace_candidate，席位切换打桩为 usage_based。"""
        with (
            patch.object(
                workspace_membership,
                "_ensure_candidate_usage_based",
                return_value={"raw_seat_type": "usage_based"},
            ),
            patch.object(workspace_membership.db, "get_export_internal_config", return_value={
                "cpa": {"cpa_url": "http://cpa.local", "cpa_mgmt_key": "key"},
            }),
            patch.object(
                exporter, "delete_cpa_auth_file", return_value={"ok": True, "deleted": True}
            ) as delete_mock,
        ):
            result = workspace_membership.trash_workspace_candidate(1, email, reason=reason)
        return result, delete_mock

    def test_quota_zero_trash_deletes_cpa_credential_and_releases_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                db.save_registered({"email": "member@example.com", "access_token": "tok"})
                db.assign_workspace_candidates(1, ["member@example.com"])
                leased = db.lease_cpa_proxy(1, "member@example.com", POOL)
                self.assertEqual(db.cpa_proxy_lease_counts(1)[leased], 1)

                result, delete_mock = self._trash("member@example.com", "quota_zero")

                self.assertTrue(result["ok"])
                delete_mock.assert_called_once()
                self.assertEqual(delete_mock.call_args[0][1], "member@example.com")
                # 代理绑定已释放 → 计数 -1。
                self.assertEqual(db.cpa_proxy_lease_counts(1), {})

    def test_delete_failure_still_releases_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                db.save_registered({"email": "member@example.com", "access_token": "tok"})
                db.assign_workspace_candidates(1, ["member@example.com"])
                db.lease_cpa_proxy(1, "member@example.com", POOL)

                with (
                    patch.object(
                        workspace_membership,
                        "_ensure_candidate_usage_based",
                        return_value={"raw_seat_type": "usage_based"},
                    ),
                    patch.object(workspace_membership.db, "get_export_internal_config", return_value={
                        "cpa": {"cpa_url": "http://cpa.local", "cpa_mgmt_key": "key"},
                    }),
                    patch.object(
                        exporter, "delete_cpa_auth_file", return_value={"ok": False, "error": "HTTP 500"}
                    ),
                ):
                    result = workspace_membership.trash_workspace_candidate(
                        1, "member@example.com", reason="quota_403"
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(db.cpa_proxy_lease_counts(1), {})

    def test_manual_trash_does_not_touch_cpa(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                db.save_registered({"email": "member@example.com", "access_token": "tok"})
                db.assign_workspace_candidates(1, ["member@example.com"])
                leased = db.lease_cpa_proxy(1, "member@example.com", POOL)

                result, delete_mock = self._trash("member@example.com", "manual_trash")

                self.assertTrue(result["ok"])
                delete_mock.assert_not_called()
                # 手动入箱不释放代理——账号可能恢复，凭证仍在 CPA。
                self.assertEqual(db.cpa_proxy_lease_counts(1)[leased], 1)


class CpaStaticProxyGateTests(unittest.TestCase):
    """开关默认关闭：只有显式启用且池非空时才租代理写 proxy_url。"""

    def _run_export(self, ws_settings: dict):
        captured = {}
        def fake_run_exports(cred, *, cpa_cfg=None, sub2api_cfg=None, **kwargs):
            captured["cpa_cfg"] = cpa_cfg
            return {"cpa": {"ok": True}, "sub2api": None, "any_attempted": True}

        with (
            patch.object(registrar.db, "get_export_internal_config", return_value={
                "cpa": {"enabled": True, "cpa_url": "http://cpa.local", "cpa_mgmt_key": "k"},
                "sub2api": {},
            }),
            patch.object(registrar.db, "get_workspace_settings", return_value={
                "auto_push_cpa_enabled": True,
                "auto_push_sub2api_enabled": False,
                **ws_settings,
            }),
            patch.object(registrar.db, "lease_cpa_proxy", return_value="socks5://home-a:1080") as lease_mock,
            patch.object(exporter, "run_exports", side_effect=fake_run_exports),
        ):
            registrar._try_export_to_panels(
                "run-x", {"email": "a@example.com", "access_token": "t"},
                {"workspace_db_id": 1},
            )
        return lease_mock, captured.get("cpa_cfg") or {}

    def test_disabled_by_default_no_lease(self):
        lease_mock, cpa_cfg = self._run_export({"cpa_static_proxy_pool": POOL})
        lease_mock.assert_not_called()
        self.assertNotIn("credential_proxy_url", cpa_cfg)

    def test_enabled_leases_and_injects_proxy(self):
        lease_mock, cpa_cfg = self._run_export({
            "cpa_static_proxy_enabled": True,
            "cpa_static_proxy_pool": POOL,
        })
        lease_mock.assert_called_once_with(1, "a@example.com", POOL.strip())
        self.assertEqual(cpa_cfg.get("credential_proxy_url"), "socks5://home-a:1080")

    def test_enabled_but_empty_pool_no_lease(self):
        lease_mock, cpa_cfg = self._run_export({
            "cpa_static_proxy_enabled": True,
            "cpa_static_proxy_pool": "",
        })
        lease_mock.assert_not_called()
        self.assertNotIn("credential_proxy_url", cpa_cfg)


class CpaBoundProxyReuseTests(unittest.TestCase):
    """已绑定 CPA 家宽代理的账号：额度查询/重登录固定走同一出口。"""

    def test_quota_lease_prefers_bound_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                bound = db.lease_cpa_proxy(1, "a@example.com", "socks5://home-a:1080")
                pool = app._candidate_quota_proxy_pool("socks5://pool-a:1080")
                with patch.object(app.proxy_usage, "record_lease") as rec:
                    got = app._lease_candidate_quota_proxy(
                        pool, workspace_id=1, email="a@example.com", detail="t"
                    )
                self.assertEqual(got, bound)
                rec.assert_called_once_with(bound, "quota", "t")

    def test_quota_lease_falls_back_to_pool_when_bound_excluded(self):
        # 绑定代理刚失败的重试显式排除它 → 回退到候选人代理池。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                bound = db.lease_cpa_proxy(1, "a@example.com", "socks5://home-a:1080")
                pool = app._candidate_quota_proxy_pool("socks5://pool-a:1080")
                got = app._lease_candidate_quota_proxy(
                    pool,
                    workspace_id=1,
                    email="a@example.com",
                    detail="t",
                    exclude_proxy=bound,
                )
                self.assertEqual(got, "socks5://pool-a:1080")

    def test_quota_lease_pool_when_no_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                pool = app._candidate_quota_proxy_pool("socks5://pool-a:1080")
                got = app._lease_candidate_quota_proxy(
                    pool, workspace_id=1, email="nobind@example.com", detail="t"
                )
                self.assertEqual(got, "socks5://pool-a:1080")

    def test_candidate_options_include_bound_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                db.save_registered({"email": "m@example.com", "access_token": "tok"})
                db.save_registered({"email": "n@example.com", "access_token": "tok"})
                db.assign_workspace_candidates(1, ["m@example.com", "n@example.com"])
                bound = db.lease_cpa_proxy(1, "m@example.com", "socks5://u:p@home-a:1080")

                rows = {r["email"]: r for r in db.list_workspace_candidate_options(1)}
                self.assertEqual(rows["m@example.com"]["cpa_proxy"], bound)
                self.assertEqual(rows["n@example.com"]["cpa_proxy"], "")

    def test_get_cpa_proxy_lease_for_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                bound = db.lease_cpa_proxy(1, "x@example.com", "socks5://home-a:1080")
                self.assertEqual(db.get_cpa_proxy_lease_for_email("x@example.com"), bound)
                self.assertEqual(db.get_cpa_proxy_lease(1, "x@example.com"), bound)
                self.assertEqual(db.get_cpa_proxy_lease_for_email("none@example.com"), "")
                self.assertEqual(db.get_cpa_proxy_lease(2, "x@example.com"), "")

    def test_public_relogin_prefers_bound_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                bound = db.lease_cpa_proxy(7, "a@example.com", "socks5://home-a:1080")
                account = {"email": "a@example.com", "access_token": "t"}
                normalized = {"email": "a@example.com", "chatgpt_account_id": "ws-1"}
                cfg = {"retry_count": 0, "login_timeout": 10}
                pool = public_relogin.ProxyLeasePool(["socks5://pool-a:1080"])
                with (
                    patch.object(
                        app.public_relogin,
                        "relogin_account",
                        return_value={"email": "a@example.com", "chatgpt_account_id": "ws-1"},
                    ) as relogin,
                    patch.object(app.proxy_usage, "record_lease") as rec,
                ):
                    result = app._run_public_relogin_account(account, normalized, cfg, pool)
                self.assertTrue(result["ok"])
                relogin.assert_called_once()
                self.assertEqual(relogin.call_args.kwargs["proxy"], bound)
                rec.assert_called_once_with(bound, "login", "public_401_relogin_cpa_bound")

    def test_public_relogin_explicit_proxy_wins_over_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup_db(path)
                db.lease_cpa_proxy(7, "a@example.com", "socks5://home-a:1080")
                account = {
                    "email": "a@example.com",
                    "access_token": "t",
                    "proxy": "socks5://own:1080",
                }
                normalized = {"email": "a@example.com", "chatgpt_account_id": "ws-1"}
                cfg = {"retry_count": 0, "login_timeout": 10}
                pool = public_relogin.ProxyLeasePool(["socks5://pool-a:1080"])
                with patch.object(
                    app.public_relogin,
                    "relogin_account",
                    return_value={"email": "a@example.com", "chatgpt_account_id": "ws-1"},
                ) as relogin:
                    result = app._run_public_relogin_account(account, normalized, cfg, pool)
                self.assertTrue(result["ok"])
                self.assertEqual(relogin.call_args.kwargs["proxy"], "socks5://own:1080")


if __name__ == "__main__":
    unittest.main()
