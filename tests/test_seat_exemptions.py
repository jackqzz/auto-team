"""席位豁免额度/暂留预测：专属池(=已购席位)优先、共享底池(10)竞争、
耗尽触发暂留、每笔消耗 72h 滚动返还。

锁定四件事：池归属按时间序重放（专属→底池→暂留）、跨轨互不流通、
72h 窗口滚动返还、释放动作三入口（kick/leave/降级）都会记账。
"""
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from webui import db, workspace_membership


@contextmanager
def _tmp_db(tmp):
    with patch.object(db, "DB_PATH", Path(tmp) / "test.db"):
        db.init_db()
        db.import_workspace_sessions(
            "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
            "----socks5://127.0.0.1:1080"
        )
        yield


def _seed_master(seats_default=2, seats_prolite=0):
    db.update_workspace_seat_info(
        1,
        seats_default=seats_default,
        seats_default_entitled=seats_default,
        seats_prolite=seats_prolite,
        seats_prolite_entitled=seats_prolite,
    )


class ExemptionLedgerTests(unittest.TestCase):
    def test_record_and_list_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                eid = db.record_seat_exemption_event(1, "a@x.com", "m1", "default", "kick")
                self.assertGreater(eid, 0)
                events = db.list_seat_exemption_events(1)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["track"], "default")
                self.assertEqual(events[0]["action"], "kick")
                # since 过滤
                self.assertEqual(db.list_seat_exemption_events(1, since=time.time() + 1), [])

    def test_dedicated_pool_first_then_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                _seed_master(seats_default=2)
                now = time.time()
                for i in range(3):
                    db.record_seat_exemption_event(1, f"k{i}@x.com", track="default", action="kick", created_at=now - i)
                state = workspace_membership.seat_exemption_state(1, now=now)
                t = state["tracks"]["default"]
                # 前 2 个吃专属池，第 3 个落底池
                self.assertEqual(t["dedicated_used"], 2)
                self.assertEqual(t["dedicated_remaining"], 0)
                self.assertEqual(state["base_used"], 1)
                self.assertEqual(state["base_remaining"], 9)
                # 安全释放 = 专属剩0 + 底池剩9
                self.assertEqual(t["safe_releases"], 9)

    def test_retention_triggered_when_pools_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                _seed_master(seats_default=0)
                now = time.time()
                # 无专属池：前 10 个吃底池，第 11 个触发暂留
                for i in range(11):
                    db.record_seat_exemption_event(1, f"k{i}@x.com", track="default", action="kick", created_at=now - i)
                state = workspace_membership.seat_exemption_state(1, now=now)
                self.assertEqual(state["base_used"], 10)
                self.assertEqual(state["base_remaining"], 0)
                self.assertEqual(state["tracks"]["default"]["safe_releases"], 0)
                self.assertEqual(state["tracks"]["default"]["retained_count"], 1)
                retained = [e for e in state["events"] if e["pool"] == "retained"]
                self.assertEqual(len(retained), 1)

    def test_base_pool_competition_across_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                _seed_master(seats_default=0, seats_prolite=0)
                now = time.time()
                # 6 标准先吃底池，6 高级后吃：4 落底池 + 2 触发暂留
                for i in range(6):
                    db.record_seat_exemption_event(1, f"s{i}@x.com", track="default", action="kick", created_at=now - 200 + i)
                for i in range(6):
                    db.record_seat_exemption_event(1, f"p{i}@x.com", track="prolite", action="kick", created_at=now - 100 + i)
                state = workspace_membership.seat_exemption_state(1, now=now)
                self.assertEqual(state["base_used"], 10)
                self.assertEqual(state["tracks"]["prolite"]["retained_count"], 2)
                self.assertEqual(state["tracks"]["default"]["retained_count"], 0)

    def test_rolling_window_expiry_returns_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                _seed_master(seats_default=0)
                now = time.time()
                # 一个 71h 前的事件仍在窗口内；一个 73h 前的已滚出返还
                db.record_seat_exemption_event(1, "in@x.com", track="default", action="kick", created_at=now - 71 * 3600)
                db.record_seat_exemption_event(1, "out@x.com", track="default", action="kick", created_at=now - 73 * 3600)
                state = workspace_membership.seat_exemption_state(1, now=now)
                self.assertEqual(state["base_used"], 1)
                self.assertEqual(state["base_remaining"], 9)
                # 下次返还 = 71h前事件 +72h = 约 1h 后
                nxt = state["tracks"]["default"]["next_release_at"]
                self.assertIsNotNone(nxt)
                self.assertAlmostEqual(nxt, now + 3600, delta=10)


class ExemptionRecordingHookTests(unittest.TestCase):
    """三条释放路径都要进台账：踢出、主动退出、降级到 Codex。"""

    def _seed_candidate(self, seat_type="default"):
        # 母号已由 _tmp_db 导入（id=1），这里只补候选关系
        db.save_registered({"email": "m@x.com", "access_token": "at"})
        db.assign_workspace_candidates(1, ["m@x.com"])
        db.update_workspace_candidate_member(1, "m@x.com", "user-1", seat_type)

    def test_remove_member_records_kick_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                self._seed_candidate("prolite")
                fake_response = types.SimpleNamespace(status_code=200, text="", json=lambda: {})
                with patch.object(
                    workspace_membership, "create_workspace_http_session",
                    return_value=(object(), {"workspace_id": "ws-1", "access_token": "t"}),
                ), patch.object(
                    workspace_membership, "_workspace_admin_request", return_value=fake_response
                ):
                    workspace_membership.remove_member(1, "m@x.com", "user-1")
                events = db.list_seat_exemption_events(1)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["action"], "kick")
                # 候选行 seat_type=prolite → 归到高级轨
                self.assertEqual(events[0]["track"], "prolite")

    def test_kick_already_gone_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                self._seed_candidate("default")
                with patch.object(
                    workspace_membership, "create_workspace_http_session",
                    return_value=(object(), {"workspace_id": "ws-1", "access_token": "t"}),
                ), patch.object(
                    workspace_membership, "fetch_candidate_seats", return_value={}
                ):
                    workspace_membership.remove_member(1, "m@x.com", "")
                self.assertEqual(db.list_seat_exemption_events(1), [])

    def test_member_leave_records_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                self._seed_candidate("default")
                fake_response = types.SimpleNamespace(status_code=200, text="", json=lambda: {})
                session = type("S", (), {"delete": lambda *_a, **_k: fake_response})()
                with patch.object(
                    workspace_membership.db, "get_workspace_master",
                    return_value={"workspace_id": "ws-1"},
                ), patch.object(
                    workspace_membership, "_candidate_quota_session",
                    return_value=(session, {}),
                ):
                    workspace_membership.member_leave_workspace(1, "m@x.com", "user-1", proxy="p")
                events = db.list_seat_exemption_events(1)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["action"], "leave")

    def test_usage_based_release_not_recorded(self):
        """Codex 席位不是付费轨，释放不消耗豁免。"""
        with tempfile.TemporaryDirectory() as tmp:
            with _tmp_db(tmp):
                self._seed_candidate("usage_based")
                workspace_membership._record_seat_exemption_event(1, "m@x.com", track="usage_based")
                self.assertEqual(db.list_seat_exemption_events(1), [])


if __name__ == "__main__":
    unittest.main()
