import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import src.liveness as liveness


DELIM = "\u203d"


class Handler(BaseHTTPRequestHandler):
    hits = 0

    def do_HEAD(self):
        Handler.hits += 1
        path = self.path
        location = None
        if path.startswith("/alive"):
            code = 200
        elif path.startswith("/gone"):
            code = 410
        elif path.startswith("/missing"):
            code = 404
        elif path.startswith("/throttle"):
            code = 503
        elif path.startswith("/short"):
            code = 302
            location = "/alive/from-short"
        elif path.startswith("/removed"):
            code = 302
            location = "/"
        elif path == "/":
            code = 200
        else:
            code = 500
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class LivenessTest(unittest.TestCase):
    def setUp(self):
        Handler.hits = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def write_dump(self, specs):
        path = os.path.join(self.tmp, "data.csv")
        lines = [DELIM.join(["website", "url", "title", "tags", "upload_date", "video_id"])]
        for i, (site, suffix) in enumerate(specs):
            lines.append(DELIM.join([site, self.url(suffix), "t", "tag", "01/01/2020", str(i)]))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def test_classify_and_wilson(self):
        self.assertEqual(liveness.classify(200, "https://host/video/1"), "alive")
        self.assertEqual(liveness.classify(200, "https://host/"), "dead")
        self.assertEqual(liveness.classify(302, "https://host/video/1"), "dead")
        self.assertEqual(liveness.classify(404, "https://host/missing"), "dead")
        self.assertEqual(liveness.classify(410, "https://host/gone"), "dead")
        self.assertEqual(liveness.classify(503, "https://host/video/1"), "unknown")
        self.assertEqual(liveness.classify(None), "unknown")
        rate, low, high = liveness.wilson(50, 100)
        self.assertAlmostEqual(rate, 0.5)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)
        self.assertLess(high - low, liveness.wilson(5, 10)[2] - liveness.wilson(5, 10)[1])

    def test_neyman_spends_budget_on_uncertain_sites(self):
        population = {"Sure": 100000, "Mixed": 100000, "Unknown": 10000}
        pilot = {"Sure": (95, 5), "Mixed": (50, 50)}
        quotas = liveness.allocate(population, budget=300, pilot=pilot, floor=20)
        self.assertGreater(quotas["Mixed"], quotas["Sure"])
        self.assertGreaterEqual(quotas["Unknown"], 20)
        self.assertLessEqual(sum(quotas.values()), 300 + 5)

    def test_early_stop_skips_once_interval_is_narrow(self):
        specs = [("Alpha", "/alive/%d" % i) for i in range(80)]
        path = self.write_dump(specs)
        population, samples = liveness.sample_urls(path, {"Alpha": 0}, seed=1)
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        done = liveness.check_all(
            samples,
            os.path.join(out, "results.jsonl"),
            workers=4,
            timeout=5,
            connections=2,
            margin=0.25,
            full=False,
        )
        decided = sum(1 for row in done.values() if row["verdict"] == "alive")
        self.assertGreaterEqual(decided, 30)
        self.assertLess(decided, 80)
        self.assertLess(Handler.hits, 80)

    def test_full_mode_checks_everything_and_reuses_result(self):
        specs = [
            ("Alpha", "/alive/1"),
            ("Alpha", "/missing/1"),
            ("Alpha", "/throttle/1"),
            ("Beta", "/alive/2"),
            ("Beta", "/gone/1"),
        ]
        path = self.write_dump(specs)
        population, samples = liveness.sample_urls(path, {}, seed=1)
        self.assertEqual(population, {"Alpha": 3, "Beta": 2})
        out = os.path.join(self.tmp, "out2")
        os.makedirs(out)
        results = os.path.join(out, "results.jsonl")
        done = liveness.check_all(samples, results, workers=4, timeout=5, connections=2, margin=0.03, full=True)
        self.assertEqual(done[("Alpha", self.url("/alive/1"))]["verdict"], "alive")
        self.assertEqual(done[("Alpha", self.url("/missing/1"))]["verdict"], "dead")
        self.assertEqual(done[("Beta", self.url("/gone/1"))]["verdict"], "dead")
        self.assertEqual(done[("Alpha", self.url("/throttle/1"))]["verdict"], "unknown")
        hits_after_first = Handler.hits
        again = liveness.check_all(samples, results, workers=4, timeout=5, connections=2, margin=0.03, full=True)
        self.assertEqual(again[("Alpha", self.url("/throttle/1"))]["verdict"], "unknown")
        self.assertEqual(Handler.hits, hits_after_first + 1)
        report = liveness.write_report(out, population, again)
        self.assertIn("Alpha", report)
        with open(os.path.join(out, "summary.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
        self.assertEqual(summary["sites"]["Alpha"]["estimated_alive"], 2)
        self.assertEqual(summary["sites"]["Beta"]["estimated_alive"], 1)


    def test_throttle_cuts_site_concurrency(self):
        specs = [("Alpha", "/throttle/%d" % i) for i in range(12)]
        path = self.write_dump(specs)
        _population, samples = liveness.sample_urls(path, {"Alpha": 0}, seed=1)
        out = os.path.join(self.tmp, "out3")
        os.makedirs(out)
        done = liveness.check_all(
            samples,
            os.path.join(out, "results.jsonl"),
            workers=8,
            timeout=5,
            connections=2,
            margin=0.0,
            full=True,
        )
        self.assertEqual(len(done), 12)
        self.assertTrue(all(row["verdict"] == "unknown" for row in done.values()))
        self.assertEqual(row_status_count(done, 503), 12)


    def test_redirect_to_video_is_alive_and_redirect_home_is_dead(self):
        specs = [("Alpha", "/short/1"), ("Alpha", "/removed/1"), ("Alpha", "/alive/direct")]
        path = self.write_dump(specs)
        _population, samples = liveness.sample_urls(path, {"Alpha": 0}, seed=1)
        out = os.path.join(self.tmp, "out4")
        os.makedirs(out)
        done = liveness.check_all(
            samples,
            os.path.join(out, "results.jsonl"),
            workers=2,
            timeout=5,
            connections=2,
            margin=0.0,
            full=True,
        )
        self.assertEqual(done[("Alpha", self.url("/short/1"))]["verdict"], "alive")
        self.assertTrue(done[("Alpha", self.url("/short/1"))]["final"].endswith("/alive/from-short"))
        self.assertEqual(done[("Alpha", self.url("/removed/1"))]["verdict"], "dead")
        self.assertEqual(done[("Alpha", self.url("/alive/direct"))]["verdict"], "alive")

def row_status_count(done, status):
    return sum(1 for row in done.values() if row["status"] == status)


if __name__ == "__main__":
    unittest.main()
