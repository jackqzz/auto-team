import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


class RegisterTraceTests(unittest.TestCase):
    def _db(self, tmp):
        return patch.object(db, "DB_PATH", Path(tmp) / "test.db")

    def test_failure_counts_and_last_error_persist_without_registered_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.record_register_failure("a@example.com", "run1", "boom-1", "network")
                db.record_register_failure("a@example.com", "run2", "boom-2", "account")

                t = db.get_register_trace("a@example.com")

                self.assertEqual(t["fail_count"], 2)
                self.assertEqual(t["last_fail_error"], "boom-2")
                self.assertEqual(t["last_fail_category"], "account")
                self.assertEqual(t["last_run_id"], "run2")
                self.assertTrue(t["last_fail_at"])
                # 从没成功过的账号也有追溯行，且 registered 表不受影响
                self.assertIsNone(t["registered_at"])
                self.assertIsNone(db.get_registered("a@example.com"))

    def test_success_writes_registration_facts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.record_register_success(
                    "a@example.com", "run1", mode="camoufox",
                    ip="1.2.3.4", region="US",
                    timezone="America/New_York", language="en-US",
                )
                first_at = db.get_register_trace("a@example.com")["registered_at"]
                self.assertTrue(first_at)

                # 后续一次「真注册」重跑：新值覆盖，但 registered_at 保最早
                db.record_register_success(
                    "a@example.com", "run2", mode="protocol",
                    ip="5.6.7.8", region="DE",
                )
                t = db.get_register_trace("a@example.com")
                self.assertEqual(t["register_mode"], "protocol")
                self.assertEqual(t["register_ip"], "5.6.7.8")
                self.assertEqual(t["register_region"], "DE")
                self.assertEqual(t["registered_at"], first_at)
                self.assertEqual(t["register_timezone"], "America/New_York")

    def test_login_like_success_never_overwrites_registration_facts(self):
        """camoufox 注册的号之后跑协议登录：注册方式/IP 不能被刷掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.record_register_success(
                    "a@example.com", "run1", mode="camoufox",
                    ip="1.2.3.4", region="US",
                )
                # register_event=False：仅登录/已有账号补齐链
                db.record_register_success(
                    "a@example.com", "run2", register_event=False,
                )
                t = db.get_register_trace("a@example.com")
                self.assertEqual(t["register_mode"], "camoufox")
                self.assertEqual(t["register_ip"], "1.2.3.4")
                self.assertEqual(t["register_region"], "US")
                self.assertEqual(t["last_run_id"], "run2")
                self.assertTrue(t["last_success_at"])

    def test_fail_count_survives_later_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.record_register_failure("a@example.com", "run1", "otp timeout", "network")
                db.record_register_failure("a@example.com", "run2", "otp timeout", "network")
                db.record_register_success(
                    "a@example.com", "run3", mode="protocol", ip="9.9.9.9", region="JP",
                )

                t = db.get_register_trace("a@example.com")
                self.assertEqual(t["fail_count"], 2)
                self.assertEqual(t["last_fail_error"], "otp timeout")
                self.assertEqual(t["register_ip"], "9.9.9.9")

    def test_list_registered_and_get_registered_expose_trace_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.save_registered({"email": "a@example.com", "access_token": "tok",
                                    "register_mode": "camoufox"})
                db.record_register_success(
                    "a@example.com", "run1", mode="camoufox",
                    ip="1.2.3.4", region="US",
                )
                db.record_register_failure("b@example.com", "run9", "x", "account")
                db.save_registered({"email": "b@example.com", "access_token": "tok"})

                rows = {r["email"]: r for r in db.list_registered()}
                self.assertEqual(rows["a@example.com"]["register_ip"], "1.2.3.4")
                self.assertEqual(rows["a@example.com"]["register_region"], "US")
                self.assertEqual(rows["a@example.com"]["fail_count"], 0)
                self.assertTrue(rows["a@example.com"]["registered_at"])
                self.assertEqual(rows["b@example.com"]["fail_count"], 1)
                self.assertEqual(rows["b@example.com"]["last_fail_error"], "x")

                detail = db.get_registered("a@example.com")
                self.assertEqual(detail["register_ip"], "1.2.3.4")
                self.assertEqual(detail["fail_count"], 0)

    def test_registered_filters_still_work_with_trace_join(self):
        """JOIN 之后 _registered_conditions 的筛选语义不能变。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.save_registered({"email": "with-at@example.com", "access_token": "tok"})
                db.save_registered({"email": "without-at@example.com", "access_token": ""})
                db.record_register_failure("without-at@example.com", "r", "e", "account")

                self.assertEqual(
                    [r["email"] for r in db.list_registered(filter_rt="has_at")],
                    ["with-at@example.com"],
                )
                self.assertEqual(db.count_registered(filter_rt="has_at"), 1)

    def test_init_db_backfills_registered_and_runs_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                # 先建出「老库」结构以外再补 —— 直接建新库再手工填老数据行
                db.init_db()
                db.save_registered({"email": "old@example.com", "access_token": "tok",
                                    "register_mode": "camoufox",
                                    "register_timezone": "America/Chicago"})
                db.create_run("r1", "old@example.com", "/tmp/r1.log")
                db.finish_run("r1", "failed", "err1", category="network")
                db.create_run("r2", "gone@example.com", "/tmp/r2.log")
                db.finish_run("r2", "failed", "err2", category="account")

                db.init_db()  # 第二次 init 触发回填

                t = db.get_register_trace("old@example.com")
                self.assertEqual(t["register_mode"], "camoufox")
                self.assertEqual(t["register_timezone"], "America/Chicago")
                self.assertTrue(t["registered_at"])
                self.assertEqual(t["fail_count"], 1)

                # registered 已删的号也能从 runs 回填出失败历史
                t2 = db.get_register_trace("gone@example.com")
                self.assertEqual(t2["fail_count"], 1)

                # 幂等：再跑一次不能把 fail_count 翻倍
                db.init_db()
                self.assertEqual(db.get_register_trace("old@example.com")["fail_count"], 1)

    def test_save_registered_empty_mode_preserves_camoufox(self):
        """登录 run 不带 register_mode 重存凭证时，camoufox 不被刷成空/别的。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.save_registered({"email": "a@example.com", "access_token": "t1",
                                    "register_mode": "camoufox"})
                db.save_registered({"email": "a@example.com", "access_token": "t2"})
                self.assertEqual(
                    db.get_registered("a@example.com")["register_mode"], "camoufox"
                )

    def test_import_marks_trace_mode_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.import_2fa_registered("i@example.com----pw123")
                t = db.get_register_trace("i@example.com")
                self.assertEqual(t["register_mode"], "import")
                self.assertTrue(t["registered_at"])

    def test_list_and_count_traces_with_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.save_registered({"email": "ok@example.com", "access_token": "t"})
                con = db._conn()
                con.execute(
                    "UPDATE registered SET group_name='g1' WHERE email='ok@example.com'"
                )
                con.commit()
                db.record_register_success("ok@example.com", "r1", mode="camoufox",
                                           ip="1.1.1.1", region="US")
                db.record_register_failure("bad@foo.com", "r2", "e", "network")
                db.record_register_failure("bad2@foo.com", "r3", "e", "account")

                all_rows = db.list_register_traces(limit=100)
                self.assertEqual(len(all_rows), 3)
                self.assertEqual(db.count_register_traces(), 3)

                # email 子串过滤
                foo = db.list_register_traces(email="foo.com")
                self.assertEqual({r["email"] for r in foo},
                                 {"bad@foo.com", "bad2@foo.com"})
                self.assertEqual(db.count_register_traces(email="foo.com"), 2)

                # only_failed 只列有失败的
                failed = db.list_register_traces(only_failed=True)
                self.assertEqual({r["email"] for r in failed},
                                 {"bad@foo.com", "bad2@foo.com"})
                self.assertEqual(db.count_register_traces(only_failed=True), 2)

                # 附带 registered 概要（分组/是否有凭证行）
                ok = next(r for r in all_rows if r["email"] == "ok@example.com")
                self.assertEqual(ok["group_name"], "g1")
                self.assertEqual(ok["has_credentials"], 1)
                bad = next(r for r in all_rows if r["email"] == "bad@foo.com")
                self.assertEqual(bad["group_name"], "")
                self.assertEqual(bad["has_credentials"], 0)

    def test_trace_full_falls_back_to_registered_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                # 账号在册、无 trace 行（极端兜底）：仍能查出概要
                db.save_registered({"email": "plain@example.com", "access_token": "t",
                                    "register_mode": "protocol"})
                con = db._conn()
                con.execute("DELETE FROM register_trace WHERE email='plain@example.com'")
                con.commit()

                row = db.get_register_trace_full("plain@example.com")
                self.assertIsNotNone(row)
                self.assertEqual(row["register_mode"], "protocol")
                self.assertEqual(row["has_credentials"], 1)
                self.assertIsNone(db.get_register_trace_full("nobody@example.com"))

    def test_batch_query_returns_map_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._db(tmp):
                db.init_db()
                db.record_register_success("a@example.com", "r1", mode="protocol")
                db.record_register_failure("b@example.com", "r2", "e", "account")

                items = db.get_register_traces_by_emails(
                    ["a@example.com", "b@example.com", "none@example.com", " A@EXAMPLE.COM "]
                )
                self.assertEqual(set(items), {"a@example.com", "b@example.com"})
                self.assertEqual(items["a@example.com"]["register_mode"], "protocol")
                self.assertEqual(items["b@example.com"]["fail_count"], 1)
                self.assertNotIn("none@example.com", items)
                self.assertEqual(db.get_register_traces_by_emails([]), {})


if __name__ == "__main__":
    unittest.main()
