import concurrent.futures
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ledger import HOUR, INITIAL_WINDOW, OFFER_TTL, Ledger, Rejected, performance
from serve import handler


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.sqlite"
        self.now = 1_800_000_000
        self.ledger = Ledger(self.path, max_initial=2, author_hourly=1, clock=lambda: self.now)

    def admit(self, post=1, author=50, eligible=True):
        return self.ledger.publish(post, author, self.now, eligible)

    def offer(self, viewer, request=None, post=1, stage=0):
        self.ledger.replace_audience(viewer, self.now, [])
        return self.ledger.offer(viewer, request or viewer, 10, [{"post_id": post, "stage": stage}])

    def confirm(self, viewer, request=None, post=1, viewed=False):
        return self.ledger.confirm(viewer, request or viewer, post, viewed, 1000 if viewed else 0, False)

    def test_admission_and_capacity(self):
        self.assertEqual(self.admit(), {"state": "active"})
        self.assertEqual(self.admit(), {"state": "active"})
        self.assertEqual(self.admit(2, 60), {"state": "active"})
        with self.assertRaisesRegex(Rejected, "capacity"):
            self.admit(3, 70)
        self.now += INITIAL_WINDOW
        self.assertEqual(self.ledger.report()[0]["state"], "missed")
        self.assertEqual(self.admit(3, 70), {"state": "active"})

    def test_author_rate_limit_and_withdrawal(self):
        self.admit()
        self.offer(2)
        with self.assertRaisesRegex(Rejected, "capacity"):
            self.admit(2, 50)
        self.admit(eligible=False)
        self.assertEqual(self.admit(), {"state": "withdrawn"})
        with self.assertRaisesRegex(Rejected, "no longer active"):
            self.confirm(2)

    def test_requires_known_unfamiliar_audience(self):
        self.admit()
        self.assertEqual(self.ledger.candidates(2), [])
        self.ledger.replace_audience(2, self.now, [50])
        self.assertEqual(self.ledger.candidates(2), [])
        self.assertEqual(self.ledger.offer(2, 3, 10, [{"post_id": 1, "stage": 0}]), [])
        self.ledger.replace_audience(50, self.now, [])
        self.assertEqual(self.ledger.candidates(50), [])
        self.ledger.replace_audience(4, self.now, [])
        self.assertEqual(len(self.ledger.candidates(4)), 1)
        self.now += 25 * HOUR
        self.assertEqual(self.ledger.candidates(4), [])

    def test_offer_and_confirmation_are_distinct_and_persistent(self):
        self.admit()
        self.assertEqual(self.offer(2), [1])
        self.assertEqual(self.offer(2), [1])
        self.assertEqual(self.offer(2, 3), [])
        self.assertEqual(self.ledger.report()[0]["delivered"], 0)
        restarted = Ledger(self.path, clock=lambda: self.now)
        self.assertEqual(restarted.confirm(2, 2, 1, True, 500, True), {"counted": True})
        self.assertEqual(self.confirm(2), {"counted": True})
        report = restarted.report()[0]
        self.assertEqual((report["delivered"], report["views"], report["engagements"]), (1, 1, 1))

    def test_expired_lease_and_request_retries(self):
        self.admit()
        self.offer(2)
        self.now += OFFER_TTL
        with self.assertRaisesRegex(Rejected, "no longer active"):
            self.confirm(2)
        self.offer(2)  # The response is replayed without reopening the lease.
        with self.assertRaisesRegex(Rejected, "no longer active"):
            self.confirm(2)
        self.assertEqual(self.offer(2, 3), [1])
        with self.assertRaisesRegex(Rejected, "No matching"):
            self.confirm(2)
        self.assertTrue(self.confirm(2, 3)["counted"])

    def test_slot_limit_and_request_payload(self):
        self.admit()
        self.ledger.replace_audience(2, self.now, [])
        post = {"post_id": 1, "stage": 0}
        with self.assertRaisesRegex(Rejected, "slot budget"):
            self.ledger.offer(2, 2, 9, [post])
        self.assertEqual(self.ledger.offer(2, 2, 10, [post]), [1])
        with self.assertRaisesRegex(Rejected, "Conflicting"):
            self.ledger.offer(2, 2, 20, [post])

    def test_concurrent_leases_do_not_overbook(self):
        self.admit()
        for viewer in range(100, 299):
            self.assertEqual(self.offer(viewer), [1])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            remaining = list(pool.map(self.offer, range(300, 304)))
        self.assertEqual(sum(map(len, remaining)), 1)
        self.assertEqual(self.ledger.report()[0]["initial_delivered"], 0)

    def test_quota_and_successive_expansion(self):
        self.admit()
        for viewer in range(100, 300):
            self.assertEqual(self.offer(viewer), [1])
            self.confirm(viewer, viewed=True)
        report = self.ledger.report()[0]
        self.assertTrue(report["initial_fulfilled"])
        self.assertEqual(report["initial_delivered"], 200)
        self.assertEqual(self.ledger.candidates(100), [])
        self.ledger.replace_audience(300, self.now, [])
        self.assertEqual(self.ledger.candidates(300)[0]["stage"], 1)
        for viewer in range(300, 500):
            self.assertEqual(self.offer(viewer, stage=1), [1])
            self.confirm(viewer, viewed=False)
        report = self.ledger.report()[0]
        self.assertEqual(report["state"], "complete")
        self.assertEqual(report["delivered"], 400)
        self.assertEqual(report["unfamiliar"], 400)

    def test_deadline_and_scoring(self):
        self.admit()
        self.now += INITIAL_WINDOW - 1
        self.offer(2)
        self.now += 1
        with self.assertRaisesRegex(Rejected, "no longer active"):
            self.confirm(2)
        self.assertEqual(self.ledger.report()[0]["state"], "missed")
        self.assertGreater(performance(120, 200, HOUR)[0], performance(60, 200, HOUR)[0])
        self.assertGreater(performance(120, 200, HOUR)[0], performance(120, 200, 20 * HOUR)[0])
        self.assertGreater(performance(60, 100, HOUR)[1], performance(6, 10, HOUR)[1])

    def test_http_contract(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), handler(self.ledger, "test")) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()

            def post(path, token="test", **body):
                request = Request(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    data=json.dumps(body).encode(),
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                with urlopen(request, timeout=2) as response:
                    return json.load(response)

            try:
                with self.assertRaises(HTTPError) as unauthorized:
                    post("/report", token="wrong")
                self.assertEqual(unauthorized.exception.code, 401)
                self.assertEqual(
                    post("/publish", post_id=2**64 - 1, author_id=50, published_at=self.now, eligible=True),
                    {"state": "active"},
                )
                post("/audience", viewer_id=2, updated_at=self.now, established_author_ids=[])
                plans = post("/candidates", viewer_id=2)
                self.assertEqual(plans, [{"post_id": 2**64 - 1, "author_id": 50, "stage": 0}])
                self.assertEqual(post("/offer", viewer_id=2, request_id=3, slots=10,
                                      posts=[{"post_id": 2**64 - 1, "stage": 0}]), [2**64 - 1])
                self.assertEqual(post("/confirm", viewer_id=2, request_id=3, post_id=2**64 - 1,
                                      viewed=True, dwell_ms=100, engaged=False), {"counted": True})
                self.assertEqual(post("/report")[0]["delivered"], 1)
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    unittest.main()
