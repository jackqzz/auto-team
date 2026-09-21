import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db


class ImportRelaySuffixTests(unittest.TestCase):
    def _import(self, text, *, suffix="", kind="icloud_relay"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "test.db"
        with patch.object(db, "DB_PATH", path):
            db.init_db()
            result = db.import_accounts(
                text, kind, group_name=None, relay_suffix=suffix,
            )
            return result, {e: db.get_account(e) for e in self._emails(text)}

    @staticmethod
    def _emails(text):
        return [
            line.split("----")[0].strip().lower()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_suffix_appended_to_every_relay_url(self):
        text = (
            "a@example.com----pw1----https://relay.example.com/messages/t1\n"
            "b@example.com----pw2----https://relay.example.com/messages/t2"
        )
        result, rows = self._import(text, suffix="?json=1")
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(
            rows["a@example.com"]["relay_url"],
            "https://relay.example.com/messages/t1?json=1",
        )
        self.assertEqual(
            rows["b@example.com"]["relay_url"],
            "https://relay.example.com/messages/t2?json=1",
        )

    def test_empty_suffix_preserves_url(self):
        result, rows = self._import(
            "a@example.com----pw----https://relay.example.com/messages/t1",
        )
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(
            rows["a@example.com"]["relay_url"],
            "https://relay.example.com/messages/t1",
        )

    def test_suffix_appended_verbatim_to_url_with_query(self):
        result, rows = self._import(
            "a@example.com----pw----https://relay.example.com/api?uid=x1",
            suffix="&format=json",
        )
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(
            rows["a@example.com"]["relay_url"],
            "https://relay.example.com/api?uid=x1&format=json",
        )

    def test_reimport_with_same_suffix_does_not_duplicate(self):
        text = "a@example.com----pw----https://relay.example.com/messages/t1"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                first = db.import_accounts(text, "icloud_relay", relay_suffix="?json=1")
                second = db.import_accounts(text, "icloud_relay", relay_suffix="?json=1")
                self.assertEqual(first["inserted"], 1)
                self.assertEqual(second["skipped"], 1)
                relay = db.get_account("a@example.com")["relay_url"]
                self.assertEqual(relay.count("?json=1"), 1)

    def test_suffix_ignored_for_rows_without_relay(self):
        account = {
            "email": "o@example.com",
            "password": "mail-pass",
            "client_id": "client",
            "refresh_token": "refresh",
            "kind": "outlook",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with patch.object(db, "DB_PATH", path), patch.object(
                db, "parse_lines", return_value=[account],
            ):
                db.init_db()
                result = db.import_accounts(
                    "ignored", "outlook", relay_suffix="?json=1",
                )
                self.assertEqual(result["inserted"], 1)
                self.assertEqual(db.get_account("o@example.com")["relay_url"], "")


if __name__ == "__main__":
    unittest.main()
