"""个人空间（Free 账号池）。

成员是 registered 里的免费账号本身；凭证就是个人 token。覆盖：
- 划入/移出/垃圾箱 CRUD；
- 额度调度器（401 重登、零额度入箱、代理池为空跳过、死线程自愈）；
- 个人空间推送覆盖（CPA/Sub2API）；
- 手动端点的凭证入队。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


def _seed_account(email, *, token=True):
    db.save_registered({
        "email": email,
        "access_token": f"at-{email}" if token else "",
        "password": "pw",
    })


class _StopAfterWaits:
    def __init__(self, n=1):
        self.n = n
        self.waits = []
        self._stopped = False

    def is_set(self):
        return self._stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if len(self.waits) >= self.n:
            self._stopped = True
        return self._stopped

    def set(self):
        self._stopped = True


class PersonalCandidateCrudTests(unittest.TestCase):
    def _seed(self):
        db.init_db()
        for e in ("a@example.com", "b@example.com"):
            _seed_account(e)

    def test_assign_only_takes_registered_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed()
                added = db.assign_personal_candidates(
                    ["a@example.com", "ghost@example.com"]
                )
                rows = db.list_personal_candidates()
        self.assertEqual(added, 1)
        self.assertEqual([r["email"] for r in rows], ["a@example.com"])
        self.assertEqual(rows[0]["credential_status"], "personal_credential")

    def test_reassign_restores_trashed_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed()
                db.assign_personal_candidates(["a@example.com"])
                db.update_personal_candidate_trash(
                    "a@example.com", status="trashed", reason="quota_zero"
                )
                self.assertEqual(db.count_personal_candidates("active"), 0)
                db.assign_personal_candidates(["a@example.com"])
                row = db.get_personal_candidate("a@example.com")
                active = db.count_personal_candidates("active")
        self.assertEqual(row["trash_status"], "active")
        self.assertEqual(active, 1)

    def test_remove_keeps_registered_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed()
                db.assign_personal_candidates(["a@example.com"])
                db.remove_personal_candidates(["a@example.com"])
                remaining = db.list_personal_candidates()
                registered = db.get_registered("a@example.com")
        self.assertEqual(remaining, [])
        self.assertIsNotNone(registered)

    def test_quota_payload_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed()
                db.assign_personal_candidates(["a@example.com"])
                db.update_personal_quota(
                    "a@example.com", {"primary": {"used_percent": 42}}
                )
                row = db.list_personal_candidates()[0]
                import json as _json
                self.assertEqual(
                    _json.loads(row["quota_json"])["primary"]["used_percent"], 42
                )


class PersonalQuotaWorkerTests(unittest.TestCase):
    def _settings(self, **over):
        s = dict(app._PERSONAL_SPACE_DEFAULTS)
        s.update({
            "quota_enabled": True,
            "quota_interval_minutes": 30,
            "proxy_pool": "socks5://u:p@127.0.0.1:1080",
            "quota_network_retries": 0,
        })
        s.update(over)
        return s

    def _seed_pool(self, *emails):
        db.init_db()
        for e in emails:
            _seed_account(e)
        db.assign_personal_candidates(list(emails))

    def _run_batch(self, settings, stop, fetch_result=None, fetch_error=None):
        calls = []

        def fake_fetch(email, *, proxy, network_retries):
            calls.append(email)
            if fetch_error:
                raise fetch_error
            return fetch_result or {
                "primary": {"used_percent": 10, "window_seconds": 18000},
                "secondary": {"used_percent": 20, "window_seconds": 604800},
            }

        class _Leases:
            def lease(self, exclude, *, task_type, task_detail, skip_cooldown):
                return "socks5://u:p@127.0.0.1:1080", 0, 1

        with (
            patch.object(app, "_candidate_quota_proxy_pool", return_value=_Leases()),
            patch.object(app, "_drain_due_personal_trash", return_value=0),
            patch.object(
                app.workspace_membership, "fetch_personal_quota",
                side_effect=fake_fetch,
            ),
        ):
            import logging
            app._personal_quota_batch(settings, stop, logging.getLogger("test"))
        return calls

    def test_batch_refreshes_active_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed_pool("a@example.com", "b@example.com")
                calls = self._run_batch(self._settings(), _StopAfterWaits(1))
        self.assertEqual(sorted(calls), ["a@example.com", "b@example.com"])

    def test_trashed_and_tokenless_members_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                _seed_account("a@example.com")
                _seed_account("b@example.com")
                _seed_account("c@example.com", token=False)  # 无个人凭证
                db.assign_personal_candidates(
                    ["a@example.com", "b@example.com", "c@example.com"]
                )
                db.update_personal_candidate_trash("b@example.com", status="trashed")
                calls = self._run_batch(self._settings(), _StopAfterWaits(1))
        self.assertEqual(calls, ["a@example.com"])

    def test_zero_quota_schedules_trash(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed_pool("a@example.com")
                with patch.object(
                    app, "_schedule_personal_trash", return_value=True
                ) as sched:
                    self._run_batch(
                        self._settings(trash_zero_delay_minutes=5),
                        _StopAfterWaits(1),
                        fetch_result={
                            "primary": {"used_percent": 100, "window_seconds": 18000},
                            "secondary": {"used_percent": 100, "window_seconds": 604800},
                        },
                    )
        sched.assert_called_once_with(
            "a@example.com", reason="quota_zero", delay_seconds=300
        )

    def test_401_triggers_relogin_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed_pool("a@example.com")
                seen = []

                def fetch(email, *, proxy, network_retries):
                    seen.append(email)
                    if len(seen) == 1:
                        raise app.workspace_membership.QuotaUnauthorized("401")
                    return {"primary": {"used_percent": 5, "window_seconds": 18000}}

                class _Leases:
                    def lease(self, exclude, *, task_type, task_detail, skip_cooldown):
                        return "socks5://u:p@127.0.0.1:1080", 0, 1

                with (
                    patch.object(app, "_candidate_quota_proxy_pool", return_value=_Leases()),
                    patch.object(app, "_drain_due_personal_trash", return_value=0),
                    patch.object(
                        app.workspace_membership, "fetch_personal_quota",
                        side_effect=fetch,
                    ),
                    patch.object(
                        app, "_wait_and_relogin_personal", return_value=True
                    ) as relogin,
                ):
                    import logging
                    app._personal_quota_batch(
                        self._settings(relogin_on_401=True),
                        _StopAfterWaits(1), logging.getLogger("test"),
                    )
        relogin.assert_called_once()
        self.assertEqual(seen, ["a@example.com", "a@example.com"])

    def test_401_without_relogin_setting_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed_pool("a@example.com")
                calls = self._run_batch(
                    self._settings(relogin_on_401=False),
                    _StopAfterWaits(1),
                    fetch_error=app.workspace_membership.QuotaUnauthorized("401"),
                )
        self.assertEqual(calls, ["a@example.com"])  # 失败即跳过，不重登

    def test_empty_proxy_pool_skips_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed_pool("a@example.com")
                with patch.object(
                    app, "_candidate_quota_proxy_pool",
                    side_effect=ValueError("empty"),
                ), patch.object(
                    app.workspace_membership, "fetch_personal_quota"
                ) as fetch:
                    import logging
                    app._personal_quota_batch(
                        self._settings(), _StopAfterWaits(1),
                        logging.getLogger("test"),
                    )
        fetch.assert_not_called()

    def test_worker_updates_next_at_and_stops_when_disabled(self):
        stop = _StopAfterWaits(1)
        next_at = {"next_at": 0}
        calls = []
        settings = self._settings()
        with (
            patch.object(app, "_personal_space_settings", side_effect=lambda: settings),
            patch.object(app, "_personal_quota_batch", side_effect=lambda *a, **k: calls.append(1)),
        ):
            app._personal_quota_worker(stop, next_at)
        self.assertEqual(calls, [1])
        self.assertGreater(next_at["next_at"], 0)

    def test_batch_exception_does_not_kill_worker(self):
        stop = _StopAfterWaits(2)
        settings = self._settings()
        with (
            patch.object(app, "_personal_space_settings", side_effect=lambda: settings),
            patch.object(
                app, "_personal_quota_batch", side_effect=RuntimeError("db boom")
            ),
        ):
            app._personal_quota_worker(stop, {"next_at": 0})
        self.assertEqual(len(stop.waits), 2)


class PersonalTrashTests(unittest.TestCase):
    def test_apply_trash_marks_and_deletes_cpa(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                _seed_account("a@example.com")
                db.assign_personal_candidates(["a@example.com"])
                db.update_personal_settings({
                    "auto_push_cpa_url": "https://cpa.local",
                    "auto_push_cpa_mgmt_key": "k",
                })
                with patch("webui.exporter.delete_cpa_auth_file") as delete:
                    delete.return_value = {"ok": True, "deleted": True}
                    app._apply_personal_trash("a@example.com", reason="quota_zero")
                    row = db.get_personal_candidate("a@example.com")
        delete.assert_called_once()
        cfg = delete.call_args[0][0]
        self.assertEqual(cfg["cpa_url"], "https://cpa.local")
        self.assertEqual(row["trash_status"], "trashed")

    def test_due_trash_drainer(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                _seed_account("a@example.com")
                _seed_account("b@example.com")
                db.assign_personal_candidates(["a@example.com", "b@example.com"])
                db.update_personal_candidate_trash(
                    "a@example.com", status="scheduled", reason="quota_zero",
                    due_at=time.time() - 1,
                )
                db.update_personal_candidate_trash(
                    "b@example.com", status="scheduled", reason="quota_zero",
                    due_at=time.time() + 3600,
                )
                with patch.object(
                    app, "_apply_personal_trash", return_value={"ok": True}
                ) as apply_:
                    n = app._drain_due_personal_trash()
        self.assertEqual(n, 1)
        apply_.assert_called_once_with("a@example.com", reason="quota_zero")


class PersonalPushCfgTests(unittest.TestCase):
    def test_personal_overrides_merge_into_cfg(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                db.update_personal_settings({
                    "auto_push_cpa_url": "https://cpa.ps",
                    "auto_push_cpa_mgmt_key": "pk",
                    "auto_push_cpa_priority": -3,
                    "auto_push_sub2api_url": "https://s2.ps",
                    "auto_push_sub2api_api_key": "sk",
                    "auto_push_sub2api_group_ids": "7",
                })
                cfg = app._personal_push_cfg(app._personal_space_settings())
        self.assertEqual(cfg["cpa"]["cpa_url"], "https://cpa.ps")
        self.assertEqual(cfg["cpa"]["cpa_priority"], -3)
        self.assertEqual(cfg["sub2api"]["sub2api_url"], "https://s2.ps")
        self.assertEqual(cfg["sub2api"]["sub2api_group_ids"], "7")

    def test_disabled_targets_stay_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                db.update_personal_settings({
                    "auto_push_cpa_enabled": False,
                    "auto_push_sub2api_enabled": False,
                })
                cfg = app._personal_push_cfg(app._personal_space_settings())
        self.assertFalse(cfg["cpa"]["enabled"])
        self.assertFalse(cfg["sub2api"]["enabled"])


class PersonalSettingsEndpointTests(unittest.TestCase):
    def test_settings_roundtrip_and_partial_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                db.init_db()
                req = app.PersonalSpaceSettingsReq(
                    quota_interval_minutes=45, relogin_on_401=True
                )
                resp = app.api_personal_settings_save(req)
                self.assertTrue(resp["ok"])
                self.assertEqual(resp["settings"]["quota_interval_minutes"], 45)
                self.assertTrue(resp["settings"]["relogin_on_401"])
                # 未携带字段保持默认
                self.assertEqual(resp["settings"]["concurrency"], 2)
                # 部分更新不覆盖已存值
                app.api_personal_settings_save(
                    app.PersonalSpaceSettingsReq(concurrency=4)
                )
                final = app._personal_space_settings()
        self.assertEqual(final["quota_interval_minutes"], 45)
        self.assertEqual(final["concurrency"], 4)


if __name__ == "__main__":
    unittest.main()
