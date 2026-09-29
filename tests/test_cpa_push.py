import unittest
from unittest.mock import patch

from webui import app, db, exporter


class CpaPushTests(unittest.TestCase):
    def test_bulk_push_continues_after_one_account_fails(self):
        rows = [
            {"email": "one@example.com", "refresh_token": "rt-one"},
            {"email": "two@example.com", "refresh_token": ""},
        ]

        def fake_export(cred, **_kwargs):
            if cred["email"] == "one@example.com":
                return {"cpa": {"ok": True, "file_name": "one@example.com.json"}}
            return {"cpa": {"ok": False, "error": "缺少 refresh_token"}}

        with patch.object(exporter, "run_exports", side_effect=fake_export):
            results = exporter.push_many_to_cpa(
                rows, {"cpa_url": "https://cpa.example", "cpa_mgmt_key": "key"}
            )

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])

    def test_bulk_push_injects_per_credential_proxy(self):
        rows = [{"email": "a@example.com"}, {"email": "b@example.com"}]
        seen = []

        def fake_export(cred, *, cpa_cfg=None, **_kwargs):
            seen.append(cpa_cfg.get("credential_proxy_url"))
            return {"cpa": {"ok": True}}

        with patch.object(exporter, "run_exports", side_effect=fake_export):
            exporter.push_many_to_cpa(
                rows,
                {"cpa_url": "https://cpa.example", "cpa_mgmt_key": "key"},
                credential_proxy_for=lambda c: f"socks5://{c['email']}:1080",
            )

        self.assertEqual(
            seen, ["socks5://a@example.com:1080", "socks5://b@example.com:1080"]
        )

    def test_bulk_push_empty_lease_writes_no_proxy(self):
        rows = [{"email": "a@example.com"}]
        seen = []

        def fake_export(cred, *, cpa_cfg=None, **_kwargs):
            seen.append("credential_proxy_url" in (cpa_cfg or {}))
            return {"cpa": {"ok": True}}

        with patch.object(exporter, "run_exports", side_effect=fake_export):
            exporter.push_many_to_cpa(
                rows,
                {"cpa_url": "https://cpa.example", "cpa_mgmt_key": "key"},
                credential_proxy_for=lambda c: "",
            )

        self.assertEqual(seen, [False])


class WorkspaceManualCpaPushTests(unittest.TestCase):
    """空间候选人的手动 CPA 推送套用「空间专属号池推送」配置。"""

    def _run(self, ws_settings, global_cpa=None, invoke_resolver=False):
        captured = {}
        req = app.BulkPushReq(emails=["a@example.com"], workspace_id=1)
        global_cfg = {
            "cpa": (
                {"cpa_url": "http://global-cpa", "cpa_mgmt_key": "global-key"}
                if global_cpa is None
                else global_cpa
            )
        }

        def fake_push(rows, cfg, **kwargs):
            captured["cfg"] = cfg
            captured["kwargs"] = kwargs
            return [{"email": r.get("email"), "ok": True} for r in rows]

        with (
            patch.object(db, "get_export_internal_config", return_value=global_cfg),
            patch.object(db, "get_workspace_settings", return_value=ws_settings),
            patch.object(
                db,
                "list_workspace_credentials_by_emails",
                return_value=[{"email": "a@example.com", "access_token": "t"}],
            ),
            patch.object(db, "lease_cpa_proxy", return_value="socks5://home-a:1080") as lease_mock,
            patch.object(exporter, "push_many_to_cpa", side_effect=fake_push),
        ):
            out = app.api_push_registered_to_cpa(req)
            resolver = captured["kwargs"].get("credential_proxy_for")
            if invoke_resolver and resolver is not None:
                captured["leased"] = resolver({"email": "a@example.com"})
        return captured, lease_mock, out

    def test_workspace_url_and_key_override_global(self):
        captured, _, out = self._run({
            "auto_push_cpa_url": "http://space-cpa",
            "auto_push_cpa_mgmt_key": "space-key",
        })
        self.assertTrue(out["ok"])
        self.assertEqual(captured["cfg"]["cpa_url"], "http://space-cpa")
        self.assertEqual(captured["cfg"]["cpa_mgmt_key"], "space-key")

    def test_blank_workspace_settings_fall_back_to_global(self):
        captured, _, _ = self._run({})
        self.assertEqual(captured["cfg"]["cpa_url"], "http://global-cpa")
        self.assertEqual(captured["cfg"]["cpa_mgmt_key"], "global-key")

    def test_space_only_config_passes_validation(self):
        captured, _, out = self._run(
            {"auto_push_cpa_url": "http://space-cpa", "auto_push_cpa_mgmt_key": "space-key"},
            global_cpa={"cpa_url": "", "cpa_mgmt_key": ""},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(captured["cfg"]["cpa_url"], "http://space-cpa")

    def test_no_config_anywhere_raises_400(self):
        with self.assertRaises(app.HTTPException):
            self._run({}, global_cpa={"cpa_url": "", "cpa_mgmt_key": ""})

    def test_static_proxy_enabled_leases_per_credential(self):
        pool = "socks5://home-a:1080\nsocks5://home-b:1080\n"
        captured, lease_mock, _ = self._run(
            {
                "cpa_static_proxy_enabled": True,
                "cpa_static_proxy_pool": pool,
            },
            invoke_resolver=True,
        )
        self.assertIsNotNone(captured["kwargs"]["credential_proxy_for"])
        self.assertEqual(captured["leased"], "socks5://home-a:1080")
        lease_mock.assert_called_once_with(1, "a@example.com", pool.strip())

    def test_static_proxy_disabled_no_resolver(self):
        captured, lease_mock, _ = self._run({
            "cpa_static_proxy_enabled": False,
            "cpa_static_proxy_pool": "socks5://home-a:1080",
        })
        self.assertIsNone(captured["kwargs"]["credential_proxy_for"])
        lease_mock.assert_not_called()

    def test_global_push_without_workspace_unchanged(self):
        captured = {}
        req = app.BulkPushReq(emails=["a@example.com"])

        def fake_push(rows, cfg, **kwargs):
            captured["cfg"] = cfg
            captured["kwargs"] = kwargs
            return [{"email": "a@example.com", "ok": True}]

        with (
            patch.object(db, "get_export_internal_config", return_value={
                "cpa": {"cpa_url": "http://global-cpa", "cpa_mgmt_key": "global-key"},
            }),
            patch.object(db, "get_workspace_settings") as ws_mock,
            patch.object(
                db,
                "list_registered_by_emails",
                return_value=[{"email": "a@example.com", "access_token": "t"}],
            ),
            patch.object(exporter, "push_many_to_cpa", side_effect=fake_push),
        ):
            out = app.api_push_registered_to_cpa(req)

        self.assertTrue(out["ok"])
        ws_mock.assert_not_called()
        self.assertEqual(captured["cfg"]["cpa_url"], "http://global-cpa")
        self.assertIsNone(captured["kwargs"]["credential_proxy_for"])

    def test_missing_email_reports_per_email_error(self):
        """批量推送对未找到的邮箱逐条报错，找到的照常推。"""
        req = app.BulkPushReq(
            emails=["found@example.com", "missing@example.com"], workspace_id=1,
        )
        with (
            patch.object(db, "get_export_internal_config", return_value={
                "cpa": {"cpa_url": "http://global-cpa", "cpa_mgmt_key": "global-key"},
            }),
            patch.object(db, "get_workspace_settings", return_value={}),
            patch.object(
                db,
                "list_workspace_credentials_by_emails",
                return_value=[{"email": "found@example.com", "access_token": "t"}],
            ),
            patch.object(
                exporter, "push_many_to_cpa",
                return_value=[{"email": "found@example.com", "ok": True}],
            ) as push,
        ):
            out = app.api_push_registered_to_cpa(req)
        push.assert_called_once()
        self.assertEqual(len(push.call_args.args[0]), 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["succeeded"], 1)
        missing = [r for r in out["results"] if r["email"] == "missing@example.com"]
        self.assertEqual(missing[0]["error"], "未找到注册结果")


class Sub2apiBulkPushTests(unittest.TestCase):
    def test_bulk_push_continues_after_one_account_fails(self):
        rows = [
            {"email": "one@example.com"},
            {"email": "two@example.com"},
        ]

        def fake_export(cred, **kwargs):
            if cred["email"] == "one@example.com":
                return {"sub2api": {"ok": True, "account_id": "1"}}
            return {"sub2api": {"ok": False, "error": "HTTP 500"}}

        with patch.object(exporter, "run_exports", side_effect=fake_export):
            results = exporter.push_many_to_sub2api(
                rows, {"sub2api_url": "https://s2.example", "sub2api_api_key": "key"}
            )

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])

    def test_bulk_push_forces_enabled_and_injects_proxy(self):
        seen = []

        def fake_export(cred, *, sub2api_cfg=None, **_kwargs):
            seen.append(dict(sub2api_cfg or {}))
            return {"sub2api": {"ok": True}}

        with patch.object(exporter, "run_exports", side_effect=fake_export):
            exporter.push_many_to_sub2api(
                [{"email": "a@example.com"}],
                {"sub2api_url": "https://s2.example", "sub2api_api_key": "key"},
                proxy="socks5://push:1080",
            )

        self.assertTrue(seen[0]["enabled"])
        self.assertEqual(seen[0]["proxy"], "socks5://push:1080")


class WorkspaceManualSub2apiPushTests(unittest.TestCase):
    """空间候选人的手动 Sub2API 推送套用「空间专属号池推送」配置。"""

    def _run(self, ws_settings, global_sub2api=None):
        captured = {}
        req = app.BulkPushReq(emails=["a@example.com"], workspace_id=1)
        global_cfg = {
            "sub2api": (
                {"sub2api_url": "http://global-s2", "sub2api_api_key": "global-key",
                 "sub2api_group_ids": "2"}
                if global_sub2api is None
                else global_sub2api
            )
        }

        def fake_push(rows, cfg, **kwargs):
            captured["cfg"] = cfg
            captured["kwargs"] = kwargs
            return [{"email": r.get("email"), "ok": True} for r in rows]

        with (
            patch.object(db, "get_export_internal_config", return_value=global_cfg),
            patch.object(db, "get_workspace_settings", return_value=ws_settings),
            patch.object(
                db,
                "list_workspace_credentials_by_emails",
                return_value=[{"email": "a@example.com", "access_token": "t"}],
            ),
            patch.object(exporter, "push_many_to_sub2api", side_effect=fake_push),
        ):
            out = app.api_push_registered_to_sub2api(req)
        return captured, out

    def test_workspace_url_key_groups_override_global(self):
        captured, out = self._run({
            "auto_push_sub2api_url": "http://space-s2",
            "auto_push_sub2api_api_key": "space-key",
            "auto_push_sub2api_group_ids": "7,9",
        })
        self.assertTrue(out["ok"])
        self.assertEqual(captured["cfg"]["sub2api_url"], "http://space-s2")
        self.assertEqual(captured["cfg"]["sub2api_api_key"], "space-key")
        self.assertEqual(captured["cfg"]["sub2api_group_ids"], "7,9")

    def test_blank_workspace_settings_fall_back_to_global(self):
        captured, _ = self._run({})
        self.assertEqual(captured["cfg"]["sub2api_url"], "http://global-s2")
        self.assertEqual(captured["cfg"]["sub2api_api_key"], "global-key")
        self.assertEqual(captured["cfg"]["sub2api_group_ids"], "2")

    def test_space_only_config_passes_validation(self):
        captured, out = self._run(
            {"auto_push_sub2api_url": "http://space-s2", "auto_push_sub2api_api_key": "space-key"},
            global_sub2api={"sub2api_url": "", "sub2api_api_key": ""},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(captured["cfg"]["sub2api_url"], "http://space-s2")

    def test_no_config_anywhere_raises_400(self):
        with self.assertRaises(app.HTTPException):
            self._run({}, global_sub2api={"sub2api_url": "", "sub2api_api_key": ""})

    def test_global_push_without_workspace_unchanged(self):
        captured = {}
        req = app.BulkPushReq(emails=["a@example.com"])

        def fake_push(rows, cfg, **kwargs):
            captured["cfg"] = cfg
            return [{"email": "a@example.com", "ok": True}]

        with (
            patch.object(db, "get_export_internal_config", return_value={
                "sub2api": {"sub2api_url": "http://global-s2", "sub2api_api_key": "global-key"},
            }),
            patch.object(db, "get_workspace_settings") as ws_mock,
            patch.object(
                db,
                "list_registered_by_emails",
                return_value=[{"email": "a@example.com", "access_token": "t"}],
            ),
            patch.object(exporter, "push_many_to_sub2api", side_effect=fake_push),
        ):
            out = app.api_push_registered_to_sub2api(req)

        self.assertTrue(out["ok"])
        ws_mock.assert_not_called()
        self.assertEqual(captured["cfg"]["sub2api_url"], "http://global-s2")


if __name__ == "__main__":
    unittest.main()
