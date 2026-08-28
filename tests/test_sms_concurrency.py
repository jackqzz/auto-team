import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import sms_provider
from sms_provider import SmsBowerProvider


class SmsConcurrencyTests(unittest.TestCase):
    def test_number_rent_does_not_hold_lock_for_other_workers(self):
        calls = 0

        def rent(_provider, action, service, country):
            nonlocal calls
            time.sleep(0.08)
            calls += 1
            return {
                "activationId": f"activation-{calls}",
                "phoneNumber": f"1555000{calls}",
            }

        first = SmsBowerProvider("test-key", reuse_phone_to_max=False)
        second = SmsBowerProvider("test-key", reuse_phone_to_max=False)
        with patch.object(SmsBowerProvider, "_request_number_single_action", rent):
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda provider: provider.get_number(service="dr", country="52"),
                    (first, second),
                ))
            elapsed = time.monotonic() - started

        self.assertEqual({item.activation_id for item in results}, {
            "activation-1", "activation-2",
        })
        # If the old process-wide verification lock were held across the rent
        # operation, this would be approximately 0.16s instead of one overlap.
        self.assertLess(elapsed, 0.14)
        with sms_provider._SMS_CACHE_LOCK:
            sms_provider._SMS_ACTIVE_ACTIVATIONS.clear()

    def test_concurrent_activation_is_not_put_on_shared_reuse_cache(self):
        calls = 0

        def rent(_provider, action, service, country):
            nonlocal calls
            calls += 1
            return {
                "activationId": f"activation-{calls}",
                "phoneNumber": f"1555111{calls}",
            }

        first = SmsBowerProvider("test-key", reuse_phone_to_max=True)
        second = SmsBowerProvider("test-key", reuse_phone_to_max=True)
        with patch.object(SmsBowerProvider, "_request_number_single_action", rent):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(sms_provider, "_smsbower_cache_file", return_value=Path(tmp) / "cache.json"):
                    with patch.object(sms_provider, "_SMS_CACHE", None):
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            results = list(pool.map(
                                lambda provider: provider.get_number(service="dr", country="52"),
                                (first, second),
                            ))

        self.assertEqual({item.activation_id for item in results}, {
            "activation-1", "activation-2",
        })
        self.assertLessEqual(
            sum(bool((item.metadata or {}).get("cache_managed")) for item in results),
            1,
        )
        with sms_provider._SMS_CACHE_LOCK:
            sms_provider._SMS_ACTIVE_ACTIVATIONS.clear()
            sms_provider._SMS_CACHE = None


if __name__ == "__main__":
    unittest.main()
