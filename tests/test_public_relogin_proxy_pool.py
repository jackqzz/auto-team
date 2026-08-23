import unittest

from webui.public_relogin import ProxyLeasePool


class PublicReloginProxyPoolTests(unittest.TestCase):
    def test_leases_lowest_count_and_avoids_failed_proxy(self):
        pool = ProxyLeasePool(["proxy-one", "proxy-two", "proxy-three"])

        first, first_index, _ = pool.lease()
        second, second_index, _ = pool.lease()
        retry, retry_index, _ = pool.lease(first)

        self.assertEqual((first, first_index), ("proxy-one", 0))
        self.assertEqual((second, second_index), ("proxy-two", 1))
        self.assertEqual((retry, retry_index), ("proxy-three", 2))
        self.assertEqual(
            [item["leased_count"] for item in pool.snapshot()],
            [1, 1, 1],
        )

    def test_single_dynamic_proxy_is_leased_again_for_retry(self):
        pool = ProxyLeasePool(["sid-dynamic", "sid-dynamic"])

        first, _, _ = pool.lease()
        retry, _, count = pool.lease(first)

        self.assertEqual(first, "sid-dynamic")
        self.assertEqual(retry, "sid-dynamic")
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
