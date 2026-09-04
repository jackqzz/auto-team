import unittest
from datetime import date
from unittest.mock import Mock

import auth_flow
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


class RacDateGroupTests(unittest.TestCase):
    """资料页的 Birthday 改成了 React Aria 分段控件（<div role="group">，
    内含三个 role=spinbutton）。它不是 <input>，对它调 fill() 会抛
    "Element is not an <input>..." —— 线上 6 个号 110 次重试全废在这。
    """

    @staticmethod
    def _segments(values):
        """造一组假 segment，values 是各段的 aria-valuenow。"""
        segs = []
        for value in values:
            seg = Mock()
            seg.get_attribute.return_value = value
            seg.inner_text.return_value = value or ""
            segs.append(seg)
        return segs

    def _group(self, values=("", "", ""), *, tag="div"):
        group = Mock()
        group.evaluate.return_value = tag
        segs = self._segments(values)
        locator = Mock()
        locator.count.return_value = len(segs)
        locator.nth.side_effect = lambda i: segs[i]
        group.locator.return_value = locator
        return group, segs

    def test_detects_react_aria_group(self):
        group, _ = self._group()
        self.assertTrue(auth_flow._is_rac_date_group(group))

    def test_plain_input_is_not_treated_as_date_group(self):
        """真 <input> 必须走原来的 fill() 路径，不能被误判成分段控件。"""
        group, _ = self._group(tag="input")
        self.assertFalse(auth_flow._is_rac_date_group(group))

    def test_digits_follow_us_month_day_year_order(self):
        self.assertEqual(
            auth_flow._rac_birthday_digits("1990-01-01", 3), ["01", "01", "1990"]
        )
        self.assertEqual(
            auth_flow._rac_birthday_digits("1988-12-25", 3), ["12", "25", "1988"]
        )

    def test_unknown_segment_count_is_rejected_not_guessed(self):
        with self.assertRaises(ValueError):
            auth_flow._rac_birthday_digits("1990-01-01", 2)

    def test_fill_types_each_digit_into_its_segment(self):
        """必须逐位敲键盘，让组件自己走 onChange —— 不能 evaluate 改 state。"""
        group, segs = self._group()
        auth_flow._fill_rac_date_group(group, "1990-01-01")

        self.assertEqual(
            [c.args[0] for c in segs[0].press.call_args_list], ["0", "1"]
        )
        self.assertEqual(
            [c.args[0] for c in segs[1].press.call_args_list], ["0", "1"]
        )
        self.assertEqual(
            [c.args[0] for c in segs[2].press.call_args_list], ["1", "9", "9", "0"]
        )
        for seg in segs:
            seg.click.assert_called_once()

    def test_read_reassembles_iso_date(self):
        group, _ = self._group(values=("1", "1", "1990"))
        self.assertEqual(auth_flow._read_rac_date_group(group), "1990-01-01")

    def test_read_rejects_unfilled_segments(self):
        """没填上的段读出来是 mm/dd/yyyy 占位符，必须报错而不是当成 0。

        否则会把占位状态当成填写成功，提交一个空生日。
        """
        group, _ = self._group(values=("mm", "dd", "yyyy"))
        with self.assertRaises(ValueError):
            auth_flow._read_rac_date_group(group)


if __name__ == "__main__":
    unittest.main()
