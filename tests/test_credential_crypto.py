import unittest

from webui.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
    is_encrypted_credential,
)


class CredentialCryptoTests(unittest.TestCase):
    def test_round_trip_uses_workspace_id_as_key(self):
        encoded = encrypt_credential("密码😀", "workspace-123")
        self.assertTrue(is_encrypted_credential(encoded))
        self.assertEqual(decrypt_credential(encoded, "workspace-123"), "密码😀")

    def test_wrong_key_and_tampering_are_rejected(self):
        encoded = encrypt_credential("GptPass!", "workspace-123")
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            decrypt_credential(encoded, "another-workspace")
        parts = encoded.split(":")
        parts[3] = ("A" if parts[3][0] != "A" else "B") + parts[3][1:]
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            decrypt_credential(":".join(parts), "workspace-123")

    def test_empty_and_plaintext_values_are_compatible(self):
        self.assertEqual(encrypt_credential("", "workspace-123"), "")
        self.assertEqual(decrypt_credential("", "workspace-123"), "")
        self.assertEqual(decrypt_credential("legacy-password", "workspace-123"), "legacy-password")
        encoded = encrypt_credential("legacy-password", "workspace-123")
        self.assertEqual(encrypt_credential(encoded, "workspace-123"), encoded)


if __name__ == "__main__":
    unittest.main()
