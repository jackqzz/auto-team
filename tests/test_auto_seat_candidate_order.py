"""自动补齐席位的候选注册时间排序。

auto_*_seat_candidate_order：default=默认顺序；oldest_first=注册时间长的优先；
newest_first=注册时间短的优先。切换候选与邀请候选共用同一套排序。
"""
import unittest
from unittest.mock import patch

from webui import app


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


class NormalizeOrderTests(unittest.TestCase):
    def test_aliases_and_fallback(self):
        self.assertEqual(app._normalize_auto_seat_candidate_order("oldest_first"), "oldest_first")
        self.assertEqual(app._normalize_auto_seat_candidate_order("oldest"), "oldest_first")
        self.assertEqual(app._normalize_auto_seat_candidate_order("newest_first"), "newest_first")
        self.assertEqual(app._normalize_auto_seat_candidate_order("newest"), "newest_first")
        for bad in ("", None, "bogus", "random"):
            self.assertEqual(app._normalize_auto_seat_candidate_order(bad), "default")


class SortCandidatesTests(unittest.TestCase):
    ROWS = [
        {"email": "mid@x.com", "registered_at": 200.0},
        {"email": "old@x.com", "registered_at": 100.0},
        {"email": "new@x.com", "registered_at": 300.0},
        {"email": "unknown@x.com", "registered_at": None},
    ]

    def test_oldest_first(self):
        out = app._auto_seat_sort_candidates(self.ROWS, "oldest_first")
        self.assertEqual(
            [r["email"] for r in out],
            ["old@x.com", "mid@x.com", "new@x.com", "unknown@x.com"],
        )

    def test_newest_first(self):
        out = app._auto_seat_sort_candidates(self.ROWS, "newest_first")
        self.assertEqual(
            [r["email"] for r in out],
            ["new@x.com", "mid@x.com", "old@x.com", "unknown@x.com"],
        )

    def test_default_preserves_input_order(self):
        out = app._auto_seat_sort_candidates(self.ROWS, "default")
        self.assertEqual([r["email"] for r in out], [r["email"] for r in self.ROWS])


class WorkerAppliesOrderTests(unittest.TestCase):
    """worker 每轮按设置的排序取第一个候选。"""

    def _run(self, order, candidates, invitees=None):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_standard_seat_source": "mixed",
            "auto_standard_seat_candidate_order": order,
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": 0,
        }
        calls = {"switch": [], "invite": []}
        seat_info = {
            "seats_default_entitled": 10, "seats_default": 0,
            "seats_default_available": 10, "seats_default_held": 0,
        }
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            patch.object(
                app, "_workspace_auto_standard_candidates",
                side_effect=lambda _w, seen: [c for c in candidates if c["email"] not in seen],
            ),
            patch.object(
                app, "_switch_candidate_to_default_and_verify",
                side_effect=lambda _w, c, _s: calls["switch"].append(c["email"]) or {"ok": True},
            ),
            patch.object(
                app, "_workspace_auto_invite_candidates",
                side_effect=lambda _w, seen: [c for c in (invitees or []) if c["email"] not in seen],
            ),
            patch.object(
                app, "_invite_candidates_to_seat",
                side_effect=lambda _w, batch, _s, seat: calls["invite"].append(
                    [c["email"] for c in batch]
                ) or {"ok": True, "invited": [c["email"] for c in batch]},
            ),
            patch.object(app, "_workspace_pending_invite_rows", return_value=[]),
            patch.object(app, "_workspace_members_missing_credentials", return_value=[]),
            patch.object(app, "_enqueue_workspace_credentials", return_value={"ok": True}),
        ):
            app._auto_standard_seat_worker(1, _StopAfterWaits(1))
        return calls

    def test_switch_picks_oldest_first_when_configured(self):
        calls = self._run(
            "oldest_first",
            [
                {"email": "new@x.com", "registered_at": 300.0},
                {"email": "old@x.com", "registered_at": 100.0},
            ],
        )
        self.assertEqual(calls["switch"], ["old@x.com"])

    def test_switch_picks_newest_first_when_configured(self):
        calls = self._run(
            "newest_first",
            [
                {"email": "old@x.com", "registered_at": 100.0},
                {"email": "new@x.com", "registered_at": 300.0},
            ],
        )
        self.assertEqual(calls["switch"], ["new@x.com"])

    def test_invite_batch_follows_same_order(self):
        calls = self._run(
            "oldest_first",
            [],  # 无切换候选 → mixed 落到邀请
            invitees=[
                {"email": "new@x.com", "registered_at": 300.0},
                {"email": "old@x.com", "registered_at": 100.0},
            ],
        )
        self.assertEqual(calls["invite"], [["old@x.com", "new@x.com"]])


if __name__ == "__main__":
    unittest.main()
