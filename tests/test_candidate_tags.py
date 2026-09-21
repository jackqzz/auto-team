import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


def _setup():
    db.init_db()
    db.import_workspace_sessions(
        "owner@example.com----session-token-abcdefghijklmnopqrstuvwxyz"
        "----socks5://127.0.0.1:1080"
    )
    for email in ("a@example.com", "b@example.com", "c@example.com"):
        db.save_registered({"email": email, "access_token": "token"})
    db.assign_workspace_candidates(1, ["a@example.com", "b@example.com", "c@example.com"])


class CandidateTagTests(unittest.TestCase):
    def test_add_remove_set_and_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup()

                changed = db.set_workspace_candidate_tags(
                    1, ["a@example.com", "b@example.com"], ["优质", "第二批"], mode="add",
                )
                self.assertEqual(changed, 2)
                db.set_workspace_candidate_tags(1, ["b@example.com"], ["vip"], mode="add")

                rows = db.list_workspace_candidate_options(1, tag="优质")
                self.assertEqual(
                    sorted(r["email"] for r in rows), ["a@example.com", "b@example.com"],
                )
                rows = db.list_workspace_candidate_options(1, tag="vip")
                self.assertEqual([r["email"] for r in rows], ["b@example.com"])
                b = rows[0]
                self.assertEqual(sorted(b["tags"]), ["vip", "优质", "第二批"])

                self.assertEqual(
                    db.list_workspace_candidate_tags(1), ["vip", "优质", "第二批"],
                )

                db.set_workspace_candidate_tags(1, ["b@example.com"], ["优质"], mode="remove")
                b = db.list_workspace_candidate_options(1, tag="vip")[0]
                self.assertEqual(sorted(b["tags"]), ["vip", "第二批"])

                db.set_workspace_candidate_tags(1, ["a@example.com"], [], mode="set")
                self.assertEqual(db.list_workspace_candidate_options(1, tag="优质"), [])
                self.assertEqual(db.list_workspace_candidate_tags(1), ["vip", "第二批"])

    def test_tags_do_not_leak_across_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup()
                db.import_workspace_sessions(
                    "owner2@example.com----session-token-2-abcdefghijklmnopqrstuvwxyz"
                    "----socks5://127.0.0.1:1080"
                )
                db.assign_workspace_candidates(2, ["a@example.com"])

                db.set_workspace_candidate_tags(1, ["a@example.com"], ["w1"], mode="add")
                db.set_workspace_candidate_tags(2, ["a@example.com"], ["w2"], mode="add")

                self.assertEqual(db.list_workspace_candidate_tags(1), ["w1"])
                self.assertEqual(db.list_workspace_candidate_tags(2), ["w2"])
                self.assertEqual(
                    db.list_workspace_candidate_options(2, tag="w1"), [],
                )


    def test_kick_removes_assignment_and_workspace_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup()
                db.save_workspace_credential(
                    1, {"email": "a@example.com", "access_token": "ws-token"},
                )

                counts = db.remove_workspace_assignment(1, ["a@example.com"])

                self.assertEqual(counts, {"candidates": 1, "credentials": 1})
                self.assertEqual(
                    db.list_workspace_credentials_by_emails(1, ["a@example.com"]),
                    [],
                )
                # 注册结果保留，账号还能划分到其他空间
                self.assertIsNotNone(db.list_registered_by_emails(["a@example.com"]))
                rows = db.list_workspace_candidate_options(1)
                self.assertEqual(
                    sorted(r["email"] for r in rows),
                    ["b@example.com", "c@example.com"],
                )

    def test_keyword_filters_email_and_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup()
                db.set_accounts_group(["a@example.com"], "优质组")

                # 按邮箱子串过滤
                rows = db.list_workspace_candidate_options(1, keyword="a@")
                self.assertEqual([r["email"] for r in rows], ["a@example.com"])
                self.assertEqual(db.count_workspace_candidate_options(1, keyword="a@"), 1)

                # 大小写不敏感
                rows = db.list_workspace_candidate_options(1, keyword="A@EXAMPLE")
                self.assertEqual([r["email"] for r in rows], ["a@example.com"])

                # 按分组名过滤
                rows = db.list_workspace_candidate_options(1, keyword="优质")
                self.assertEqual([r["email"] for r in rows], ["a@example.com"])

                # 通配符按字面处理，不能变成全匹配
                rows = db.list_workspace_candidate_options(1, keyword="%")
                self.assertEqual(rows, [])

                # 无命中
                self.assertEqual(
                    db.list_workspace_candidate_options(1, keyword="zzz"), [],
                )

    def test_session_plan_type_mapping(self):
        from webui import workspace_membership as wm
        cases = {
            "self_serve_business_prolite": "prolite",
            "self_serve_business": "default",
            "team": "default",
            "enterprise": "default",
            "self_serve_business_usage": "usage_based",
            "codex_seat": "usage_based",
            "free": "",
            "": "",
        }
        for plan, expected in cases.items():
            self.assertEqual(wm._seat_type_from_plan_type(plan), expected, plan)

    def test_resolve_seat_via_member_session(self):
        from webui import workspace_membership as wm
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup()
                db.save_workspace_credential(
                    1, {"email": "a@example.com", "access_token": "ws-token",
                        "session_token": "ws-session"},
                )
                # 本地缓存为空时，凭 session 的 planType 解析并回填
                with patch.object(
                    wm, "fetch_candidate_seat_via_session",
                    return_value={"seat_type": "prolite", "plan_type": "self_serve_business_prolite"},
                ) as mocked:
                    seat = wm.resolve_candidate_seat_type(1, "a@example.com", proxy="socks5://x")
                self.assertEqual(seat, "prolite")
                mocked.assert_called_once()
                row = db.get_workspace_candidate(1, "a@example.com")
                self.assertEqual(row["seat_type"], "prolite")

                # 缓存已有值时不再发 session 请求
                with patch.object(wm, "fetch_candidate_seat_via_session") as mocked2:
                    seat = wm.resolve_candidate_seat_type(1, "a@example.com", proxy="socks5://x")
                self.assertEqual(seat, "prolite")
                mocked2.assert_not_called()

    def test_kicked_candidate_moves_to_trash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                _setup()
                db.save_workspace_credential(
                    1, {"email": "a@example.com", "access_token": "ws-token"},
                )
                db.update_workspace_candidate_member(1, "a@example.com", "mem-1", "default")

                counts = db.mark_workspace_candidates_kicked(1, ["a@example.com"])

                self.assertEqual(counts, {"candidates": 1, "credentials": 1})
                # 空间凭证删掉，候选行保留但进了垃圾箱并标记已踢出
                self.assertEqual(
                    db.list_workspace_credentials_by_emails(1, ["a@example.com"]),
                    [],
                )
                self.assertEqual(
                    db.list_trashed_workspace_candidate_emails(1),
                    ["a@example.com"],
                )
                rows = db.list_workspace_candidate_options(1, trash_status="trashed")
                row = next(r for r in rows if r["email"] == "a@example.com")
                self.assertEqual(row["trash_reason"], "kicked")
                self.assertEqual(row["member_id"], "")
                self.assertEqual(row["seat_type"], "")
                self.assertEqual(row["workspace_join_status"], "not_invited")
                # 注册结果保留，恢复后仍是普通候选人
                self.assertIsNotNone(db.list_registered_by_emails(["a@example.com"]))
                db.restore_workspace_candidates_from_trash(1, ["a@example.com"])
                self.assertEqual(
                    db.list_trashed_workspace_candidate_emails(1),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
