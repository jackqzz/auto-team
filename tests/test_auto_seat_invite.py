"""席位补齐的「定时邀请」补齐来源。

空间检测到席位有缺口时，除切换已加入成员外，还可以把"已划分到空间、但尚未
受邀加入"的候选人直接邀请到目标席位——邀请即占用席位（上游计为 held），
成员在随后的空间凭证登录中自动接受邀请落为 joined，不需要先加入再切席位。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app, db


def _make_workspace(tmp):
    db.init_db()
    db.import_workspace_sessions(
        "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
        "----socks5://127.0.0.1:1080"
    )


class SeatSourceNormalizeTests(unittest.TestCase):
    def test_default_and_fallback_are_switch(self):
        for value in (None, "", "abc", "SWITCH ", 0, False):
            self.assertEqual(app._normalize_auto_seat_source(value), "switch", msg=repr(value))

    def test_supported_values_round_trip(self):
        self.assertEqual(app._normalize_auto_seat_source("invite"), "invite")
        self.assertEqual(app._normalize_auto_seat_source("INVITE-ONLY"), "invite")
        self.assertEqual(app._normalize_auto_seat_source("mixed"), "mixed")
        self.assertEqual(app._normalize_auto_seat_source("switch_invite"), "mixed")


class InviteCandidateSelectionTests(unittest.TestCase):
    """只挑「划分到空间、未受邀」的候选；已加入/已邀请/入箱/失效一律排除。"""

    def _seed(self, tmp):
        _make_workspace(tmp)
        rows = {
            "new@example.com": "not_invited",
            "pending@example.com": "pending_invite",
            "joined@example.com": "joined",
            "trashed@example.com": "not_invited",
            "invalid@example.com": "not_invited",
        }
        for email in rows:
            db.save_registered({"email": email, "access_token": f"token-{email}"})
        db.assign_workspace_candidates(1, list(rows))
        for email, status in rows.items():
            db.update_workspace_candidate_status(1, email, status)
        db.update_workspace_candidate_member(1, "joined@example.com", "mid-1", "usage_based")
        db.update_workspace_candidate_trash(1, "trashed@example.com", status="trashed", reason="quota_zero")
        db.mark_registered_permanently_invalid("invalid@example.com", "login 403")

    def test_only_not_invited_assigned_candidates_are_picked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._seed(tmp)
                rows = app._workspace_auto_invite_candidates(1)
        self.assertEqual([r["email"] for r in rows], ["new@example.com"])

    def test_seen_set_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._seed(tmp)
                rows = app._workspace_auto_invite_candidates(1, {"new@example.com"})
        self.assertEqual(rows, [])


class PendingInviteRowsTests(unittest.TestCase):
    """pending_invite 兜底口径：席位未知的按目标席位保守计入。"""

    def _seed(self, tmp):
        _make_workspace(tmp)
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            db.save_registered({"email": email, "access_token": f"token-{email}"})
        db.assign_workspace_candidates(1, ["a@example.com", "b@example.com", "c@example.com"])
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            db.update_workspace_candidate_status(1, email, "pending_invite")
        db.update_workspace_candidate_seat_type(1, "a@example.com", "default")
        db.update_workspace_candidate_seat_type(1, "b@example.com", "prolite")
        # c 没有已知席位：对两类目标席位都按占用计入。

    def test_filters_by_target_seat(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                self._seed(tmp)
                standard = app._workspace_pending_invite_rows(1, "default")
                prolite = app._workspace_pending_invite_rows(1, "prolite")
        self.assertEqual(sorted(r["email"] for r in standard), ["a@example.com", "c@example.com"])
        self.assertEqual(sorted(r["email"] for r in prolite), ["b@example.com", "c@example.com"])


class InviteRoomTests(unittest.TestCase):
    """邀请额度按 目标-在用-held 计算，再叠加上游 available 上限。"""

    def test_held_and_available_reduce_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                _make_workspace(tmp)
                seat_info = {"seats_default_held": 2, "seats_default_available": 5}
                room = app._auto_seat_invite_room(1, seat_info, target=8, current=3, seat_type="default")
                self.assertEqual(room, 3)  # 8-3-2=3，available=5 不收紧
                seat_info["seats_default_available"] = 1
                room = app._auto_seat_invite_room(1, seat_info, target=8, current=3, seat_type="default")
                self.assertEqual(room, 1)  # available 收紧到 1

    def test_local_pending_invites_cover_missing_upstream_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                _make_workspace(tmp)
                db.save_registered({"email": "a@example.com", "access_token": "t"})
                db.assign_workspace_candidates(1, ["a@example.com"])
                db.update_workspace_candidate_status(1, "a@example.com", "pending_invite")
                db.update_workspace_candidate_seat_type(1, "a@example.com", "default")
                # 上游还没把这个邀请计入 held。
                seat_info = {"seats_default_held": None, "seats_default_available": None}
                room = app._auto_seat_invite_room(1, seat_info, target=2, current=0, seat_type="default")
        self.assertEqual(room, 1)  # 2-0-本地held1=1


class InviteCandidatesToSeatTests(unittest.TestCase):
    """批量邀请 helper：一次请求发出整批，资格过滤后按剩余保护配额截断。"""

    def _seed_candidate(self, tmp, email="new@example.com"):
        _make_workspace(tmp)
        db.save_registered({"email": email, "access_token": f"token-{email}"})
        db.assign_workspace_candidates(1, [email])
        return email

    def test_invite_marks_pending_and_target_seat(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                email = self._seed_candidate(tmp)
                second = self._seed_candidate(tmp, "two@example.com")
                with patch.object(
                    app.workspace_membership, "invite_candidates", return_value={"ok": True}
                ) as invite:
                    result = app._invite_candidates_to_seat(
                        1, [{"email": email}, {"email": second}], {}, "default"
                    )
                row = db.get_workspace_candidate(1, email)
                row2 = db.get_workspace_candidate(1, second)
        self.assertTrue(result["ok"])
        self.assertEqual(result["invited"], [email, second])
        invite.assert_called_once_with(1, [email, second], seat_type="default")
        self.assertEqual(row["workspace_join_status"], "pending_invite")
        self.assertEqual(row["seat_type"], "default")
        self.assertEqual(row2["workspace_join_status"], "pending_invite")

    def test_invite_failure_leaves_status_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                email = self._seed_candidate(tmp)
                with patch.object(
                    app.workspace_membership,
                    "invite_candidates",
                    side_effect=RuntimeError("upstream 500"),
                ):
                    result = app._invite_candidates_to_seat(1, [{"email": email}], {}, "prolite")
                row = db.get_workspace_candidate(1, email)
        self.assertFalse(result["ok"])
        self.assertEqual(result["invited"], [])
        self.assertIn("邀请失败", result["error"])
        self.assertEqual(row["workspace_join_status"], "not_invited")

    def test_joined_or_pending_candidates_are_filtered_out_of_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                email = self._seed_candidate(tmp)
                other = self._seed_candidate(tmp, "other@example.com")
                db.update_workspace_candidate_status(1, email, "pending_invite")
                with patch.object(app.workspace_membership, "invite_candidates") as invite:
                    result = app._invite_candidates_to_seat(
                        1, [{"email": email}, {"email": other}], {}, "default"
                    )
                self.assertTrue(result["ok"])
                self.assertEqual(result["invited"], [other])
                self.assertEqual(
                    result["skipped"], [{"email": email, "reason": "already_pending_invite"}]
                )
                invite.assert_called_once_with(1, [other], seat_type="default")

                db.update_workspace_candidate_status(1, other, "joined")
                with patch.object(app.workspace_membership, "invite_candidates") as invite2:
                    all_skipped = app._invite_candidates_to_seat(
                        1, [{"email": email}, {"email": other}], {}, "default"
                    )
        self.assertTrue(all_skipped["ok"])
        self.assertEqual(all_skipped["invited"], [])
        self.assertEqual(len(all_skipped["skipped"]), 2)
        invite2.assert_not_called()

    def test_seat_protect_quota_is_consumed_per_invite_and_released_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                email = self._seed_candidate(tmp)
                second = self._seed_candidate(tmp, "two@example.com")
                # reserve/release 直接读 settings_json，所以保护开关要落库。
                settings = {
                    "seat_protect_enabled": True,
                    "seat_protect_threshold": 4,
                    "seat_protect_refresh_time": "00:00",
                }
                db.update_workspace_settings(1, settings)
                with patch.object(
                    app.workspace_membership, "invite_candidates", return_value={"ok": True}
                ):
                    ok = app._invite_candidates_to_seat(
                        1, [{"email": email}, {"email": second}], settings, "default"
                    )
                used = db.get_workspace_settings(1)["seat_protect_used_count"]
                self.assertTrue(ok["ok"])
                self.assertEqual(used, 2)  # 批内人数一次性计入

                for e in (email, second):
                    db.update_workspace_candidate_status(1, e, "not_invited")
                with patch.object(
                    app.workspace_membership,
                    "invite_candidates",
                    side_effect=RuntimeError("boom"),
                ):
                    failed = app._invite_candidates_to_seat(
                        1, [{"email": email}, {"email": second}], settings, "default"
                    )
                used_after = db.get_workspace_settings(1)["seat_protect_used_count"]
        self.assertFalse(failed["ok"])
        self.assertEqual(used_after, 2)  # 失败的批量邀请整体回滚保护配额

    def test_seat_protect_blocked_invite_never_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                email = self._seed_candidate(tmp, "first@example.com")
                self._seed_candidate(tmp, "second@example.com")
                settings = {
                    "seat_protect_enabled": True,
                    "seat_protect_threshold": 1,
                    "seat_protect_refresh_time": "00:00",
                }
                db.update_workspace_settings(1, settings)
                # 先用掉本周期唯一配额（reserve 自己写 window_key，直接改
                # used_count 会因窗口不符被重置），随后的批量邀请必须被拦。
                with patch.object(
                    app.workspace_membership, "invite_candidates", return_value={"ok": True}
                ) as invite:
                    first = app._invite_candidates_to_seat(
                        1, [{"email": email}], settings, "default"
                    )
                    result = app._invite_candidates_to_seat(
                        1, [{"email": "second@example.com"}], settings, "default"
                    )
        self.assertTrue(first["ok"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["invited"], [])
        self.assertTrue(result["blocked_by_protect"])
        self.assertEqual(invite.call_count, 1)

    def test_batch_is_truncated_to_remaining_protect_quota(self):
        """批内人数超出本周期剩余配额时截到剩余量，而不是整批放弃。"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
                emails = [
                    self._seed_candidate(tmp, f"n{i}@example.com") for i in range(3)
                ]
                settings = {
                    "seat_protect_enabled": True,
                    "seat_protect_threshold": 2,
                    "seat_protect_refresh_time": "00:00",
                }
                db.update_workspace_settings(1, settings)
                with patch.object(
                    app.workspace_membership, "invite_candidates", return_value={"ok": True}
                ) as invite:
                    first = app._invite_candidates_to_seat(
                        1, [{"email": emails[0]}], settings, "default"
                    )
                    result = app._invite_candidates_to_seat(
                        1, [{"email": e} for e in emails], settings, "default"
                    )
                used = db.get_workspace_settings(1)["seat_protect_used_count"]
        self.assertTrue(first["ok"])
        self.assertTrue(result["ok"])
        # 配额阈值 2、已用 1 → 本批只能再邀 1 个。
        invite.assert_called_with(1, [emails[1]], seat_type="default")
        self.assertEqual(result["invited"], [emails[1]])
        self.assertEqual(used, 2)


class _RecordingStop:
    """worker 循环控制器：计数到上限后在 wait 时收工。"""

    def __init__(self, max_actions: int):
        self.waits: list[float] = []
        self.max_actions = max_actions
        self.actions = 0
        self._stopped = False

    def is_set(self):
        return self._stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.actions >= self.max_actions:
            self._stopped = True
        return self._stopped

    def set(self):
        self._stopped = True


class _StopAfterWaits:
    """wait 次数到上限即收工——用在全程没有成功动作的用例（失败/无缺口）。"""

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


class StandardWorkerInviteModeTests(unittest.TestCase):
    """invite 模式：直接邀请未加入候选到标准席位，跳过切换链路。"""

    def _run(self, source="invite", seat_info=None, actions=2, extra_settings=None, stop=None):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_standard_seat_source": source,
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": 0,
        }
        if extra_settings:
            settings.update(extra_settings)
        stop = stop or _RecordingStop(actions)
        seat_info = seat_info or {
            "seats_default_entitled": 10,
            "seats_default": 0,
            "seats_default_available": 10,
            "seats_default_held": 0,
        }
        calls = {"invite": [], "switch": [], "enqueued": [], "switch_candidates": 0}
        pool = ["n0@example.com", "n1@example.com", "n2@example.com"]

        def invite(_ws, candidates, _settings, seat_type):
            stop.actions += 1
            emails = [c["email"] for c in candidates]
            calls["invite"].append((emails, seat_type))
            return {"ok": True, "invited": emails, "skipped": []}

        def switch(_ws, candidate, _settings):
            stop.actions += 1
            calls["switch"].append(candidate["email"])
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
                side_effect=lambda _w, _seen: calls.__setitem__(
                    "switch_candidates", calls["switch_candidates"] + 1
                )
                or [],
            ),
            patch.object(app, "_switch_candidate_to_default_and_verify", side_effect=switch),
            patch.object(
                app,
                "_workspace_auto_invite_candidates",
                side_effect=lambda _w, seen: [
                    {"email": e} for e in pool if e not in seen
                ],
            ),
            patch.object(app, "_invite_candidates_to_seat", side_effect=invite),
            patch.object(app, "_workspace_pending_invite_rows", return_value=[]),
            patch.object(app, "_workspace_members_missing_credentials", return_value=[]),
            patch.object(
                app,
                "_enqueue_workspace_credentials",
                side_effect=lambda _w, emails, _s: calls["enqueued"].append(list(emails))
                or {"ok": True},
            ),
        ):
            app._auto_standard_seat_worker(1, stop)
        return calls

    def test_invite_mode_batch_invites_up_to_target_in_one_call(self):
        """批量补齐：room 允许时一次请求发出全部候选，不是逐个串行。"""
        calls = self._run(actions=1)
        self.assertEqual(
            calls["invite"],
            [(["n0@example.com", "n1@example.com", "n2@example.com"], "default")],
        )
        # 邀请模式不查已加入候选、不走切换。
        self.assertEqual(calls["switch"], [])
        self.assertEqual(calls["switch_candidates"], 0)
        # 邀请成功后直接进空间凭证获取流程。
        self.assertEqual(
            calls["enqueued"],
            [["n0@example.com", "n1@example.com", "n2@example.com"]],
        )

    def test_batch_size_is_capped_by_invite_room(self):
        """剩余席位房间比候选少时，批次按 room 截断分批发完。"""
        calls = self._run(
            actions=2,
            seat_info={
                "seats_default_entitled": 10,
                "seats_default": 8,
                "seats_default_available": 10,
                "seats_default_held": 0,
            },
        )
        # room=10-8-0=2：第一批 2 人；seat_info 是静态 mock，下一轮 room 仍 2 →
        # 第二批补发最后 1 人，随后候选耗尽收工。
        self.assertEqual(
            calls["invite"],
            [
                (["n0@example.com", "n1@example.com"], "default"),
                (["n2@example.com"], "default"),
            ],
        )
        self.assertEqual(
            calls["enqueued"],
            [["n0@example.com", "n1@example.com", "n2@example.com"]],
        )

    def test_no_capacity_means_no_invites(self):
        calls = self._run(
            seat_info={
                "seats_default_entitled": 10,
                "seats_default": 4,
                "seats_default_available": 0,
                "seats_default_held": 6,
            },
            stop=_StopAfterWaits(2),
        )
        self.assertEqual(calls["invite"], [])
        self.assertEqual(calls["enqueued"], [])

    def test_failed_invite_is_not_enqueued_and_not_retried_same_round(self):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_standard_seat_source": "invite",
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": 0,
        }
        stop = _StopAfterWaits(2)  # 批量失败即停本轮，每轮只剩一次外层 wait
        seat_info = {
            "seats_default_entitled": 10,
            "seats_default": 0,
            "seats_default_available": 10,
            "seats_default_held": 0,
        }
        invites = []
        enqueued = []
        seen_snapshots = []

        def invitees(_w, seen):
            seen_snapshots.append(set(seen))
            return [] if "bad@example.com" in seen else [{"email": "bad@example.com"}]

        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            patch.object(app, "_workspace_auto_standard_candidates", return_value=[]),
            patch.object(app, "_switch_candidate_to_default_and_verify") as switch,
            # 批量失败即停本轮（整批失败大概率是上游/网络问题），
            # 下一个周期重新尝试。
            patch.object(app, "_workspace_auto_invite_candidates", side_effect=invitees),
            patch.object(
                app,
                "_invite_candidates_to_seat",
                side_effect=lambda _w, cs, _s, seat: invites.append(
                    [c["email"] for c in cs]
                )
                or {"ok": False, "invited": [], "error": "boom"},
            ),
            patch.object(app, "_workspace_pending_invite_rows", return_value=[]),
            patch.object(app, "_workspace_members_missing_credentials", return_value=[]),
            patch.object(
                app,
                "_enqueue_workspace_credentials",
                side_effect=lambda _w, emails, _s: enqueued.append(list(emails))
                or {"ok": True},
            ),
        ):
            app._auto_standard_seat_worker(1, stop)
        switch.assert_not_called()
        # 两轮各发一批：失败后当轮不再续发，下轮重试。
        self.assertEqual(invites, [["bad@example.com"], ["bad@example.com"]])
        self.assertEqual(seen_snapshots, [set(), set()])
        self.assertEqual(enqueued, [])

    def test_invite_blocked_by_protect_stops_round(self):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_standard_seat_source": "invite",
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": 0,
        }
        stop = _StopAfterWaits(1)  # 第一轮被保护拦住 → break → 外层 wait 收工
        seat_info = {
            "seats_default_entitled": 10,
            "seats_default": 0,
            "seats_default_available": 10,
            "seats_default_held": 0,
        }
        invites = []
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            patch.object(app, "_workspace_auto_standard_candidates", return_value=[]),
            patch.object(
                app,
                "_workspace_auto_invite_candidates",
                side_effect=lambda _w, seen: [{"email": f"n{len(seen)}@example.com"}],
            ),
            patch.object(
                app,
                "_invite_candidates_to_seat",
                side_effect=lambda _w, cs, _s, seat: invites.append(
                    [c["email"] for c in cs]
                )
                or {"ok": False, "invited": [], "blocked_by_protect": True},
            ),
            patch.object(app, "_workspace_pending_invite_rows", return_value=[]),
            patch.object(app, "_workspace_members_missing_credentials", return_value=[]),
            patch.object(app, "_enqueue_workspace_credentials") as enqueue,
        ):
            app._auto_standard_seat_worker(1, stop)
        self.assertEqual(invites, [["n0@example.com"]])  # 被保护拦住即整轮停止
        enqueue.assert_not_called()

    def test_stale_pending_invites_are_enqueued_with_fresh_ones(self):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_standard_seat_source": "invite",
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": 0,
        }
        stop = _RecordingStop(1)
        seat_info = {
            "seats_default_entitled": 10,
            "seats_default": 0,
            "seats_default_available": 10,
            "seats_default_held": 0,
        }
        enqueued = []
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            patch.object(app, "_workspace_auto_standard_candidates", return_value=[]),
            patch.object(
                app,
                "_workspace_auto_invite_candidates",
                return_value=[{"email": "fresh@example.com"}],
            ),
            patch.object(
                app,
                "_invite_candidates_to_seat",
                side_effect=lambda _w, cs, _s, seat: stop.__setattr__("actions", 1)
                or {"ok": True, "invited": [c["email"] for c in cs], "skipped": []},
            ),
            patch.object(
                app,
                "_workspace_pending_invite_rows",
                return_value=[{"email": "stale@example.com"}, {"email": "fresh@example.com"}],
            ),
            patch.object(
                app,
                "_enqueue_workspace_credentials",
                side_effect=lambda _w, emails, _s: enqueued.append(list(emails))
                or {"ok": True},
            ),
        ):
            app._auto_standard_seat_worker(1, stop)
        self.assertEqual(enqueued, [["fresh@example.com", "stale@example.com"]])

    def test_switch_mode_never_invites(self):
        """默认 switch 模式回归：候选为空也不碰邀请链路。"""
        calls = self._run(source="switch", stop=_StopAfterWaits(2))
        self.assertEqual(calls["invite"], [])
        self.assertEqual(calls["enqueued"], [])


class StandardWorkerMixedModeTests(unittest.TestCase):
    def test_mixed_switches_first_then_invites(self):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_standard_seat_source": "mixed",
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": 0,
        }
        stop = _RecordingStop(2)
        seat_info = {
            "seats_default_entitled": 10,
            "seats_default": 0,
            "seats_default_available": 10,
            "seats_default_held": 0,
        }
        order = []
        enqueued = []

        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            # 第一轮返回一个已加入候选，切换后 attempted 覆盖，第二轮空了 → 转邀请。
            patch.object(
                app,
                "_workspace_auto_standard_candidates",
                side_effect=lambda _w, seen: [{"email": "joined@example.com"}]
                if not seen
                else [],
            ),
            patch.object(
                app,
                "_switch_candidate_to_default_and_verify",
                side_effect=lambda _w, c, _s: order.append(("switch", c["email"]))
                or stop.__setattr__("actions", stop.actions + 1)
                or {"ok": True},
            ),
            patch.object(
                app,
                "_workspace_auto_invite_candidates",
                side_effect=lambda _w, seen: [{"email": "new@example.com"}] if not seen else [],
            ),
            patch.object(
                app,
                "_invite_candidates_to_seat",
                side_effect=lambda _w, cs, _s, seat: order.append(
                    ("invite", [c["email"] for c in cs], seat)
                )
                or stop.__setattr__("actions", stop.actions + 1)
                or {"ok": True, "invited": [c["email"] for c in cs], "skipped": []},
            ),
            patch.object(app, "_workspace_pending_invite_rows", return_value=[]),
            patch.object(app, "_workspace_members_missing_credentials", return_value=[]),
            patch.object(
                app,
                "_enqueue_workspace_credentials",
                side_effect=lambda _w, emails, _s: enqueued.append(list(emails))
                or {"ok": True},
            ),
        ):
            app._auto_standard_seat_worker(1, stop)

        self.assertEqual(
            order,
            [("switch", "joined@example.com"), ("invite", ["new@example.com"], "default")],
        )
        self.assertEqual(enqueued, [["joined@example.com", "new@example.com"]])


class ProliteWorkerInviteModeTests(unittest.TestCase):
    def test_prolite_invite_uses_prolite_seat_type(self):
        settings = {
            "auto_prolite_seat_enabled": True,
            "auto_prolite_seat_source": "invite",
            "auto_prolite_candidate_seat_type": "default",
            "auto_seat_interval_minutes": 5,
            "auto_seat_switch_gap_seconds": 0,
        }
        stop = _RecordingStop(1)
        seat_info = {
            "seats_prolite_entitled": 5,
            "seats_prolite": 0,
            "seats_prolite_available": 5,
            "seats_prolite_held": 0,
        }
        invites = []
        enqueued = []
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_workspace_prolite_seat_protect_exhausted", return_value=False),
            patch.object(app, "_refresh_workspace_seat_info", return_value=seat_info),
            patch.object(app.db, "update_workspace_seat_info"),
            patch.object(app, "_workspace_auto_prolite_candidates") as switch_candidates,
            patch.object(app, "_switch_candidate_to_prolite_and_verify") as switch,
            patch.object(
                app,
                "_workspace_auto_invite_candidates",
                return_value=[{"email": "p@example.com"}],
            ),
            patch.object(
                app,
                "_invite_candidates_to_seat",
                side_effect=lambda _w, cs, _s, seat: stop.__setattr__("actions", 1)
                or invites.append(([c["email"] for c in cs], seat))
                or {"ok": True, "invited": [c["email"] for c in cs], "skipped": []},
            ),
            patch.object(app, "_workspace_pending_invite_rows", return_value=[]),
            patch.object(app, "_workspace_members_missing_credentials", return_value=[]),
            patch.object(
                app,
                "_enqueue_workspace_credentials",
                side_effect=lambda _w, emails, _s: enqueued.append(list(emails))
                or {"ok": True},
            ),
        ):
            app._auto_prolite_seat_worker(1, stop)

        switch_candidates.assert_not_called()
        switch.assert_not_called()
        self.assertEqual(invites, [(["p@example.com"], "prolite")])
        self.assertEqual(enqueued, [["p@example.com"]])


class WorkerPauseGuardTests(unittest.TestCase):
    def test_paused_workspace_never_invites(self):
        settings = {
            "auto_standard_seat_enabled": True,
            "auto_standard_seat_source": "invite",
            "automation_paused": True,
            "auto_seat_interval_minutes": 5,
        }
        stop = _StopAfterWaits(1)
        with (
            patch.object(app, "_workspace_exists", return_value=True),
            patch.object(app, "_workspace_settings_snapshot", return_value=settings),
            patch.object(app, "_refresh_workspace_seat_info") as refresh,
            patch.object(app, "_invite_candidates_to_seat") as invite,
            patch.object(app, "_enqueue_workspace_credentials") as enqueue,
        ):
            app._auto_standard_seat_worker(1, stop)
        refresh.assert_not_called()
        invite.assert_not_called()
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
