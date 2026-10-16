import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import src.liveness as liveness


DELIM = "\u203d"


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        path = urlparse(self.path).path
        if path.startswith("/alive"):
            self.send_response(200)
        elif path.startswith("/gone"):
            self.send_response(410)
        elif path.startswith("/missing"):
            self.send_response(404)
        elif path.startswith("/throttle"):
            self.send_response(503)
        else:
            self.send_response(500)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class LivenessTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.server.shutdown()
        shutil.rmtree(self.tmp)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def write_dump(self):
        path = os.path.join(self.tmp, "data.csv")
        rows = ["website", "url", "title", "tags", "upload_date", "video_id"]
        lines = [DELIM.join(rows)]
        specs = [
            ("Alpha", "/alive/1"),
            ("Alpha", "/missing/1"),
            ("Alpha", "/throttle/1"),
            ("Beta", "/alive/2"),
            ("Beta", "/gone/1"),
        ]
        for site, suffix in specs:
            lines.append(DELIM.join([site, self.url(suffix), "t", "tag", "01/01/2020", suffix.strip("/")]))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def test_classify(self):
        self.assertEqual(liveness.classify(200), "alive")
        self.assertEqual(liveness.classify(302), "alive")
        self.assertEqual(liveness.classify(404), "dead")
        self.assertEqual(liveness.classify(410), "dead")
        self.assertEqual(liveness.classify(503), "unknown")
        self.assertEqual(liveness.classify(None), "unknown")

    def test_sample_keeps_every_row_when_unlimited(self):
        path = self.write_dump()
        population, samples = liveness.sample_urls(path, per_site=0, seed=1)
        self.assertEqual(population, {"Alpha": 3, "Beta": 2})
        self.assertEqual(len(samples["Alpha"]), 3)
        self.assertEqual(len(samples["Beta"]), 2)

    def test_check_retries_unknown_and_estimates(self):
        path = self.write_dump()
        population, samples = liveness.sample_urls(path, per_site=0, seed=1)
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        jobs = [(site, url) for site, urls in samples.items() for url in urls]
        results = os.path.join(out, "results.jsonl")
        done = liveness.check_all(jobs, results, workers=4, timeout=5, delay=0)
        self.assertEqual(done[("Alpha", self.url("/alive/1"))]["verdict"], "alive")
        self.assertEqual(done[("Alpha", self.url("/missing/1"))]["verdict"], "dead")
        self.assertEqual(done[("Beta", self.url("/gone/1"))]["verdict"], "dead")
        self.assertEqual(done[("Alpha", self.url("/throttle/1"))]["verdict"], "unknown")

        again = liveness.check_all(jobs, results, workers=4, timeout=5, delay=0)
        self.assertEqual(again[("Alpha", self.url("/throttle/1"))]["verdict"], "unknown")
        report = liveness.write_report(out, population, again)
        self.assertIn("Alpha", report)
        with open(os.path.join(out, "summary.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
        # Alpha: 1 alive, 1 dead, 1 unknown -> rate 1/2 of 3 rows.
        self.assertEqual(summary["sites"]["Alpha"]["estimated_alive"], 2)
        # Beta: 1 alive, 1 dead -> half of 2 rows.
        self.assertEqual(summary["sites"]["Beta"]["estimated_alive"], 1)
        self.assertIsNotNone(summary["sites"]["Alpha"]["interval_95"])

    def test_resume_skips_decided_rows(self):
        path = self.write_dump()
        _population, samples = liveness.sample_urls(path, per_site=0, seed=1)
        out = os.path.join(self.tmp, "out2")
        os.makedirs(out)
        results = os.path.join(out, "results.jsonl")
        with open(results, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "site": "Alpha",
                "url": self.url("/alive/1"),
                "verdict": "alive",
                "status": 200,
            }) + "\n")
        jobs = [(site, url) for site, urls in samples.items() for url in urls]
        done = liveness.check_all(jobs, results, workers=4, timeout=5, delay=0)
        self.assertEqual(done[("Alpha", self.url("/alive/1"))]["verdict"], "alive")
        self.assertEqual(done[("Alpha", self.url("/missing/1"))]["verdict"], "dead")


if __name__ == "__main__":
    unittest.main()
