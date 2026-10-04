"""席位切换后的空间凭证兜底。

自动补齐和手动切席位都只是「把人放上目标席位」；拿到空间凭证才算完成。
本文件覆盖三条链路：

- 切换成功后进入凭证获取队列（switch 模式）；
- 席位已到位但缺凭证的成员每轮兜底补登（含席位已满的轮次）；
- 手动切席位端点同样触发凭证获取，失败结果对前端可见。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


def _make_workspace():
    db.init_db()
    db.import_workspace_sessions(
        "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
        "----socks5://127.0.0.1:1080"
    )


class _StopAfterWaits:
    """wait 次数到上限即收工。"""

    def __init__(self, n: int = 1):
        self.n = n
        self.waits: list[float] = []
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


class MissingCredentialsQueryTests(unittest.TestCase):
    """只挑「已 joined + 已在目标席位 + 未入箱 + 无空间凭证」的成员。"""

    def _seed(self):
        _make_workspace()
        seats = {
            "gap@example.com": "default",       # 无凭证 → 命中
            "have@example.com": "default",     # 已有空间凭证 → 排除
            "other@example.com": "usage_based",  # 席位不符 → 排除
            "trash@example.com": "default",    # 已入箱 → 排除
            "outsider@example.com": "default",  # 未加入 → 排除
        }
        for email in seats:
            db.save_registered({"email": email, "access_token": f"tok-{email}"})
        db.assign_workspace_candidates(1, list(seats))
        for email, seat in seats.items():
            db.update_workspace_candidate_status(1, email, "joined")
            if email == "outsider@example.com":
                # member_id 会让 join_status 推导为 joined；未加入的候选只留席位标记。
                db.update_workspace_candidate_seat_type(1, email, seat)
            else:
                db.update_workspace_candidate_member(1, email, f"mid-{email}", seat)
        db.save_workspace_credential(1, {"email": "have@example.com", "access_token": "ws-tok"})
        db.update_workspace_candidate_trash(1, "trash@example.com", status="trashed", reason="quota_zero")
        db.update_workspace_candidate_status(1, "outsider@example.com", "not_invited")

    def test_only_seated_members_without_workspace_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed()
                self.assertEqual(
                    app._workspace_members_missing_credentials(1, "default"),
                    ["gap@example.com"],
                )
                self.assertEqual(app._workspace_members_missing_credentials(1, "prolite"), [])


class StandardWorkerCredentialTests(unittest.TestCase):
    def _run(self, *, seat_info, source="switch", candidates=None, switch_result=None,
             missing=None, enqueue_error=None, stop=None):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_standard_seat_source": source,
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": 0,
        }
        calls = {"enqueued": [], "switch": []}

        def enqueue(_w, emails, _s):
            calls["enqueued"].append(list(emails))
            if enqueue_error:
                raise RuntimeError(enqueue_error)
            return {"ok": True}

        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            patch.object(
                app,
                "_workspace_auto_standard_candidates",
                side_effect=lambda _w, seen: [
                    c for c in (candidates or []) if c["email"] not in seen
                ],
            ),
            patch.object(
                app,
                "_switch_candidate_to_default_and_verify",
                side_effect=lambda _w, c, _s: calls["switch"].append(c["email"])
                or dict(switch_result or {"ok": True}),
            ),
            patch.object(app, "_workspace_auto_invite_candidates", return_value=[]),
            patch.object(app, "_workspace_pending_invite_rows", return_value=[]),
            patch.object(
                app,
                "_workspace_members_missing_credentials",
                side_effect=lambda _w, seat: list(missing or []),
            ),
            patch.object(app, "_enqueue_workspace_credentials", side_effect=enqueue),
        ):
            app._auto_standard_seat_worker(1, stop or _StopAfterWaits(1))
        return calls

    def test_successful_switch_is_enqueued(self):
        calls = self._run(
            seat_info={"seats_default_entitled": 10, "seats_default": 0,
                       "seats_default_available": 10, "seats_default_held": 0},
            candidates=[{"email": "up@example.com"}],
        )
        self.assertEqual(calls["switch"], ["up@example.com"])
        self.assertEqual(calls["enqueued"], [["up@example.com"]])

    def test_failed_switch_is_never_enqueued(self):
        calls = self._run(
            seat_info={"seats_default_entitled": 10, "seats_default": 0,
                       "seats_default_available": 10, "seats_default_held": 0},
            candidates=[{"email": "bad@example.com"}],
            switch_result={"ok": False, "error": "insufficient_available_seats"},
        )
        self.assertEqual(calls["switch"], ["bad@example.com"])
        self.assertEqual(calls["enqueued"], [])

    def test_full_seats_still_backfills_missing_credentials(self):
        """席位已满（本轮无需切换）时，缺凭证成员也要补登——否则永远卡住。"""
        calls = self._run(
            seat_info={"seats_default_entitled": 4, "seats_default": 4,
                       "seats_default_available": 0, "seats_default_held": 0},
            missing=["gap@example.com"],
        )
        self.assertEqual(calls["switch"], [])
        self.assertEqual(calls["enqueued"], [["gap@example.com"]])

    def test_enqueue_failure_is_logged_not_raised_and_worker_continues(self):
        stop = _StopAfterWaits(2)
        calls = self._run(
            seat_info={"seats_default_entitled": 10, "seats_default": 0,
                       "seats_default_available": 10, "seats_default_held": 0},
            missing=["gap@example.com"],
            enqueue_error="候选人代理池为空",
            stop=stop,
        )
        # 入队两轮都失败，worker 不崩溃、继续按节奏等待。
        self.assertEqual(calls["enqueued"], [["gap@example.com"], ["gap@example.com"]])
        self.assertEqual(len(stop.waits), 2)


class ManualSeatEnqueueTests(unittest.TestCase):
    """手动切席位端点：切到真实席位后入队凭证获取；降级不取。"""

    def _seed_member(self):
        _make_workspace()
        db.save_registered({"email": "m@example.com", "access_token": "tok"})
        db.assign_workspace_candidates(1, ["m@example.com"])
        db.update_workspace_candidate_status(1, "m@example.com", "joined")
        db.update_workspace_candidate_member(1, "m@example.com", "mid-1", "usage_based")

    def _call(self, seat_type, enqueue_result=None, enqueue_error=None):
        enqueued = []

        def enqueue(_w, emails, _s):
            enqueued.append(list(emails))
            if enqueue_error:
                raise RuntimeError(enqueue_error)
            return enqueue_result or {"ok": True, "queued": len(emails)}

        req = app.WorkspaceCandidatesReq(workspace_id=1, emails=["m@example.com"], seat_type=seat_type)
        with (
            patch.object(app.workspace_membership, "update_member_seat_type", return_value={"ok": True}),
            patch.object(app, "_enqueue_workspace_credentials", side_effect=enqueue),
        ):
            resp = app.api_update_candidate_seat(req)
        return resp, enqueued

    def test_manual_switch_to_default_enqueues_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed_member()
                resp, enqueued = self._call("default")
                row = db.get_workspace_candidate(1, "m@example.com")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["changed"], 1)
        self.assertEqual(enqueued, [["m@example.com"]])
        self.assertEqual(resp["credential_queued"], 1)
        self.assertEqual(resp["credential_error"], "")
        self.assertEqual(row["seat_type"], "default")

    def test_manual_downgrade_never_enqueues(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed_member()
                db.update_workspace_candidate_member(1, "m@example.com", "mid-1", "default")
                resp, enqueued = self._call("usage_based")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["changed"], 1)
        self.assertEqual(enqueued, [])
        self.assertEqual(resp["credential_queued"], 0)

    def test_manual_enqueue_failure_surfaces_error_without_failing_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "t.db"):
                self._seed_member()
                resp, enqueued = self._call("default", enqueue_error="候选人代理池为空")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["changed"], 1)
        self.assertEqual(resp["credential_queued"], 0)
        self.assertEqual(resp["credential_error"], "候选人代理池为空")


if __name__ == "__main__":
    unittest.main()
