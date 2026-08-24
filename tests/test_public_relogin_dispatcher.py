import threading
import time
import unittest

from webui.public_relogin import BoundedTaskDispatcher, PublicTaskQueueFull


class PublicReloginDispatcherTests(unittest.TestCase):
    def test_global_concurrency_and_waiting_capacity(self):
        dispatcher = BoundedTaskDispatcher("test", 2)
        dispatcher.configure(concurrency=2, capacity=2)
        release = threading.Event()
        started = []
        lock = threading.Lock()

        def task(index):
            with lock:
                started.append(index)
            release.wait(2)
            return index

        futures = dispatcher.submit_many([
            lambda index=index: task(index) for index in range(4)
        ])
        deadline = time.time() + 1
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.01)

        status = dispatcher.snapshot()
        self.assertEqual(status["running"], 2)
        self.assertEqual(status["waiting"], 2)
        with self.assertRaises(PublicTaskQueueFull):
            dispatcher.submit(lambda: None)

        release.set()
        self.assertEqual(
            [future.result(2) for future in futures],
            [0, 1, 2, 3],
        )

    def test_rejects_batch_atomically(self):
        dispatcher = BoundedTaskDispatcher("test", 1)
        dispatcher.configure(concurrency=1, capacity=1)

        with self.assertRaises(PublicTaskQueueFull):
            dispatcher.submit_many([lambda: 1, lambda: 2, lambda: 3])

        status = dispatcher.snapshot()
        self.assertEqual(status["waiting"], 0)
        self.assertEqual(status["running"], 0)
        self.assertEqual(status["submitted"], 0)
        self.assertEqual(status["rejected"], 3)


if __name__ == "__main__":
    unittest.main()
