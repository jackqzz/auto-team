import unittest
from datetime import date

from auth_flow import (
    _camoufox_birthday_for_age,
    _classify_camoufox_profile_response,
    _normalize_camoufox_birthday,
    _normalize_camoufox_profile_age,
)


class CamoufoxProfileHelpersTests(unittest.TestCase):
    def test_profile_age_accepts_normal_integer(self):
        self.assertEqual(_normalize_camoufox_profile_age(36), 36)
        self.assertEqual(_normalize_camoufox_profile_age(" 55 "), 55)

    def test_profile_age_rejects_otp_age_concatenation(self):
        with self.assertRaises(ValueError):
            _normalize_camoufox_profile_age("27704436")

    def test_profile_response_terms_page_is_retryable(self):
        self.assertEqual(
            _classify_camoufox_profile_response(
                400,
                "We can't create your account due to our Terms of Use",
            ),
            "retry",
        )

    def test_profile_response_age_and_existing_account_are_not_retried_as_network_errors(self):
        self.assertEqual(
            _classify_camoufox_profile_response(400, "Enter a valid age to continue"),
            "age",
        )
        self.assertEqual(
            _classify_camoufox_profile_response(409, "user_already_exists"),
            "permanent",
        )

    def test_profile_response_server_errors_are_retryable(self):
        self.assertEqual(_classify_camoufox_profile_response(429, "Too many requests"), "retry")
        self.assertEqual(_classify_camoufox_profile_response(503, "upstream unavailable"), "retry")

    def test_birthday_field_is_generated_as_a_valid_past_date(self):
        self.assertEqual(
            _camoufox_birthday_for_age(36, today=date(2026, 8, 27)),
            "1990-01-01",
        )
        self.assertEqual(_normalize_camoufox_birthday("08/27/1990"), "1990-08-27")
        self.assertEqual(_normalize_camoufox_birthday("1990-08-27"), "1990-08-27")

    def test_birthday_field_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            _normalize_camoufox_birthday("08/27/2026-not-a-date")


if __name__ == "__main__":
    unittest.main()
