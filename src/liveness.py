#!/usr/bin/env python3
"""Liveness probe for an interrobang-delimited metadata dump.

The file is not comma-separated. Each line has six fields split on U+203D:

    website<U+203D>url<U+203D>title<U+203D>tags<U+203D>upload_date<U+203D>video_id

What matters is whether `url` still opens. This program never loads the file.
It draws a stratified sample (or walks every row), sends HEAD, and classifies:

    alive    HTTP 200-399
    dead     HTTP 404 or 410
    unknown  timeout, connection error, 403, 429, 503

503/429 on these hosts is throttling, not a removed video, so those rows stay
unknown and are retried next run. State is append-only JSONL: stop the process
and run the same command again to continue.

Examples:

    python src/liveness.py --input D:/data/data.csv --per-site 200
    python src/liveness.py --input D:/data/data.csv --per-site 0 --workers 8

`--per-site 0` checks every URL. A 24 million row file at ~15 requests/second
takes on the order of two weeks, so the default is a sample plus an estimate.
Python 3.8+, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DELIM = "\u203d"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def classify(status):
    if status is None:
        return "unknown"
    if 200 <= status < 400:
        return "alive"
    if status in (404, 410):
        return "dead"
    return "unknown"


def request_once(url, timeout):
    req = Request(url, method="HEAD", headers={"User-Agent": UA, "Accept": "text/html"})
    started = time.time()
    try:
        with urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.geturl(), int((time.time() - started) * 1000), ""
    except HTTPError as exc:
        return int(exc.code), getattr(exc, "url", "") or url, int((time.time() - started) * 1000), ""
    except Exception as exc:
        return None, "", int((time.time() - started) * 1000), type(exc).__name__


def sample_urls(path, per_site, seed):
    """Reservoir-sample `per_site` URLs from each website. per_site <= 0 keeps all."""
    rng = random.Random(seed)
    buckets = defaultdict(list)
    seen = Counter()
    rows = 0
    size = os.path.getsize(path)
    read = 0
    started = time.time()
    with open(path, "rb") as fh:
        fh.readline()
        for raw in fh:
            read += len(raw)
            rows += 1
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw:
                continue
            parts = raw.decode("utf-8", errors="replace").split(DELIM)
            if len(parts) != 6:
                continue
            website, url = parts[0], parts[1].strip()
            if not url.startswith("http"):
                continue
            seen[website] += 1
            bucket = buckets[website]
            if per_site <= 0 or len(bucket) < per_site:
                bucket.append(url)
            else:
                j = rng.randrange(seen[website])
                if j < per_site:
                    bucket[j] = url
            if rows % 5000000 == 0:
                elapsed = max(time.time() - started, 0.001)
                print(
                    "sample {:,} rows  {:.1f}%  {:.0f} MiB/s".format(
                        rows, 100.0 * read / size, (read / 1048576) / elapsed
                    ),
                    file=sys.stderr,
                    flush=True,
                )
    return dict(seen), {site: list(urls) for site, urls in buckets.items()}


def load_done(path):
    done = {}
    if not os.path.exists(path):
        return done
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            done[(row["site"], row["url"])] = row
    return done


def check_all(jobs, results_path, workers, timeout, delay):
    done = load_done(results_path)
    pending = []
    for site, url in jobs:
        prev = done.get((site, url))
        if prev is None or prev.get("verdict") == "unknown":
            pending.append((site, url))
    print("pending {:,} of {:,}".format(len(pending), len(jobs)), file=sys.stderr, flush=True)
    if not pending:
        return done

    def work(item):
        site, url = item
        if delay:
            time.sleep(delay)
        status, final, ms, error = request_once(url, timeout)
        return {
            "site": site,
            "url": url,
            "verdict": classify(status),
            "status": status,
            "final": final,
            "ms": ms,
            "error": error,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    finished = 0
    with open(results_path, "a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, item) for item in pending]
        for fut in as_completed(futures):
            row = fut.result()
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            done[(row["site"], row["url"])] = row
            finished += 1
            if finished % 50 == 0 or finished == len(pending):
                print("checked {}/{}".format(finished, len(pending)), file=sys.stderr, flush=True)
    return done


def wilson(alive, decided, z=1.96):
    if decided <= 0:
        return None
    p = alive / decided
    denom = 1 + z * z / decided
    centre = p + z * z / (2 * decided)
    margin = z * ((p * (1 - p) + z * z / (4 * decided)) / decided) ** 0.5
    return p, max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


def write_report(out_dir, population, done):
    by_site = defaultdict(Counter)
    for (site, _url), row in done.items():
        if site in population:
            by_site[site][row.get("verdict") or "unknown"] += 1

    lines = [
        "# Liveness report",
        "",
        "alive = HEAD returned HTTP 200-399. dead = HTTP 404 or 410.",
        "unknown = timeout, connection error, 403, 429 or 503. Unknown is not counted as dead,",
        "because these sites answer with 503 when they throttle. Run the same command again to retry them.",
        "",
        "| website | rows in file | checked | alive | dead | unknown | alive rate | estimated alive | 95% interval |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    summary = {}
    total_pop = 0
    total_est = 0.0
    total_lo = 0.0
    total_hi = 0.0
    any_estimate = False
    for site in sorted(population):
        pop = int(population[site])
        counts = by_site.get(site, Counter())
        alive = counts.get("alive", 0)
        dead = counts.get("dead", 0)
        unknown = counts.get("unknown", 0)
        checked = alive + dead + unknown
        interval = wilson(alive, alive + dead)
        total_pop += pop
        if interval is None:
            rate_text, est_text, band = "—", "—", "—"
            est = lo = hi = None
        else:
            any_estimate = True
            rate, lo_r, hi_r = interval
            est, lo, hi = pop * rate, pop * lo_r, pop * hi_r
            total_est += est
            total_lo += lo
            total_hi += hi
            rate_text = "{:.1%}".format(rate)
            est_text = "{:,.0f}".format(est)
            band = "{:,.0f} – {:,.0f}".format(lo, hi)
        lines.append(
            "| {} | {:,} | {:,} | {:,} | {:,} | {:,} | {} | {} | {} |".format(
                site, pop, checked, alive, dead, unknown, rate_text, est_text, band
            )
        )
        summary[site] = {
            "population": pop,
            "checked": checked,
            "alive": alive,
            "dead": dead,
            "unknown": unknown,
            "estimated_alive": None if est is None else round(est),
            "interval_95": None if lo is None else [round(lo), round(hi)],
        }
    lines.append("")
    if any_estimate and total_pop:
        lines.append(
            "Estimated live URLs: **{:,.0f}** of {:,} ({:.1%}).".format(
                total_est, total_pop, total_est / total_pop
            )
        )
        lines.append("Sum of the per-site 95% intervals: {:,.0f} – {:,.0f}.".format(total_lo, total_hi))
    else:
        lines.append("No decided rows yet, so there is no estimate.")
    lines.append("")
    lines.append("The rate uses alive / (alive + dead). Unknowns are excluded, not treated as dead.")
    lines.append("A sample estimates the file. `--per-site 0` is the only mode that checks every URL.")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "population": total_pop,
                "estimated_alive": round(total_est) if any_estimate else None,
                "interval_95": [round(total_lo), round(total_hi)] if any_estimate else None,
                "sites": summary,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    return text


def load_or_sample(args):
    os.makedirs(args.out, exist_ok=True)
    sample_path = os.path.join(args.out, "sample.json")
    if os.path.exists(sample_path):
        saved = json.load(open(sample_path, encoding="utf-8"))
        if saved.get("per_site") != args.per_site or saved.get("seed") != args.seed:
            raise SystemExit(
                "existing {} was built with per-site={} seed={}. "
                "Use that sample, or delete the output directory.".format(
                    sample_path, saved.get("per_site"), saved.get("seed")
                )
            )
        print("reusing {}".format(sample_path), file=sys.stderr)
        return saved["population"], saved["samples"]
    population, samples = sample_urls(args.input, args.per_site, args.seed)
    with open(sample_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"population": population, "samples": samples, "per_site": args.per_site, "seed": args.seed},
            fh,
            ensure_ascii=False,
        )
    print("wrote {}".format(sample_path), file=sys.stderr)
    return population, samples


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="Path to the interrobang-delimited file")
    parser.add_argument("--out", default="output/liveness", help="Directory for sample, results and report")
    parser.add_argument("--per-site", type=int, default=200, help="URLs to sample from each website (0 = every URL)")
    parser.add_argument("--seed", type=int, default=20261016)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--delay", type=float, default=0.0, help="Pause inside each worker before a request, to ease throttling")
    parser.add_argument("--passes", type=int, default=2, help="Additional retries are just another pass over rows still unknown")
    args = parser.parse_args()

    population, samples = load_or_sample(args)
    jobs = [(site, url) for site, urls in samples.items() for url in urls]
    results_path = os.path.join(args.out, "results.jsonl")
    done = {}
    for _ in range(max(1, args.passes)):
        done = check_all(jobs, results_path, args.workers, args.timeout, args.delay)
        unknown = sum(1 for row in done.values() if row.get("verdict") == "unknown")
        print("unknown remaining {:,}".format(unknown), file=sys.stderr, flush=True)
        if unknown == 0:
            break
    sys.stdout.write(write_report(args.out, population, done))


if __name__ == "__main__":
    main()
