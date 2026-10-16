#!/usr/bin/env python3
"""Liveness probe for an interrobang-delimited metadata dump.

The file is not comma-separated. Each line has six fields split on U+203D:

    website<U+203D>url<U+203D>title<U+203D>tags<U+203D>upload_date<U+203D>video_id

What matters is whether `url` still opens. Reading the file is cheap. The cost
is one network round trip per URL, so the probe spends requests where they
change the answer:

    Neyman allocation   sample size per site is proportional to rows * sqrt(p(1-p)),
                        so a site that is almost all alive gets few checks
    early stopping      a site stops once its Wilson interval is within --margin
    connection reuse    one TLS session per site instead of one handshake per URL
    adaptive throttle   HTTP 429/503 lowers that site's concurrency; 404/410 never retry

Verdicts:

    alive    final response is HTTP 200-399 and the final path is not the site root
    dead     HTTP 404 or 410, or a redirect that lands on the site root
    unknown  timeout, connection error, 403, 429, 503

Redirects are followed, up to 5 hops. A 200 that lands on `/` is a removed
video whose short link now points at the homepage, so it counts as dead.

Unknown is not dead. The same command again retries only those rows.

    python src/liveness.py --input D:/data/data.csv --per-site 1000 --margin 0.03
    python src/liveness.py --input D:/data/data.csv --per-site 0

`--per-site 0` checks every URL and disables the margin stop. Python 3.8+,
standard library only.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import socket
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

DELIM = "\u203d"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
THROTTLE = {429, 503}
DEAD = {404, 410}


def classify(status, final_url=""):
    if status is None:
        return "unknown"
    if 200 <= status < 300 and is_homepage(final_url):
        return "dead"
    if 200 <= status < 300:
        return "alive"
    if status in DEAD or status in (301, 302, 303, 307, 308):
        return "dead"
    return "unknown"


def wilson(alive, decided, z=1.96):
    """Return (rate, low, high) for a 95% Wilson interval, or None when nothing is decided."""
    if decided <= 0:
        return None
    p = alive / decided
    denom = 1 + z * z / decided
    centre = p + z * z / (2 * decided)
    margin = z * ((p * (1 - p) + z * z / (4 * decided)) / decided) ** 0.5
    return p, max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


def allocate(population, budget, pilot, floor):
    """Neyman allocation: n_i proportional to N_i * sqrt(p_i(1-p_i)).

    Sites with no pilot yet use p=0.5, the variance maximum, so the first pass
    does not starve an unknown site. Every site gets at least `floor` unless
    the budget is smaller than the number of sites.
    """
    sites = [site for site, rows in population.items() if rows > 0]
    if not sites or budget <= 0:
        return {site: 0 for site in population}
    weights = {}
    for site in sites:
        decided = pilot.get(site)
        if decided is None:
            p = 0.5
        else:
            alive, dead = decided
            total = alive + dead
            p = 0.5 if total <= 0 else alive / total
        weights[site] = population[site] * (p * (1 - p)) ** 0.5
    total_weight = sum(weights.values())
    if total_weight <= 0:
        share = budget // len(sites)
        return {site: min(population[site], share) for site in sites}

    base = min(floor, budget // len(sites)) if floor else 0
    raw = {}
    for site in sites:
        wanted = base + (budget - base * len(sites)) * weights[site] / total_weight
        raw[site] = min(population[site], max(0, int(round(wanted))))
    # Rounding can miss the budget by a few. Give the remainder to the heaviest sites.
    short = budget - sum(raw.values())
    order = sorted(sites, key=lambda s: weights[s], reverse=True)
    i = 0
    while short > 0 and any(raw[s] < population[s] for s in sites):
        site = order[i % len(order)]
        if raw[site] < population[site]:
            raw[site] += 1
            short -= 1
        i += 1
        if i > budget * len(sites) + 10:
            break
    return raw


class ConnectionPool:
    """A few keep-alive connections per origin. HEAD responses have no body to drain."""

    def __init__(self, per_origin, timeout):
        self.per_origin = per_origin
        self.timeout = timeout
        self._idle = defaultdict(list)
        self._lock = threading.Lock()

    def _new(self, scheme, host, port):
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        return conn

    def acquire(self, scheme, host, port):
        key = (scheme, host, port)
        with self._lock:
            idle = self._idle[key]
            if idle:
                return idle.pop()
        return self._new(scheme, host, port)

    def release(self, scheme, host, port, conn, reuse):
        if not reuse:
            conn.close()
            return
        key = (scheme, host, port)
        with self._lock:
            idle = self._idle[key]
            if len(idle) < self.per_origin:
                idle.append(conn)
                return
        conn.close()

    def close(self):
        with self._lock:
            groups = list(self._idle.values())
            self._idle.clear()
        for group in groups:
            for conn in group:
                conn.close()


def is_homepage(url):
    """True when the final URL has no video path left, e.g. https://host/ or https://host."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return path in ("", "/") and not parts.query


def resolve_location(current, location):
    if not location:
        return current
    if location.startswith("http://") or location.startswith("https://"):
        return location
    parts = urlsplit(current)
    origin = parts.scheme + "://" + parts.netloc
    if location.startswith("/"):
        return origin + location
    base = parts.path.rsplit("/", 1)[0]
    return origin + base + "/" + location


def request_once(url, pool, hops=5):
    """HEAD `url`, following up to `hops` redirects. Returns status, final URL, ms, error."""
    current = url
    started = time.time()
    for _ in range(hops + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None, current, int((time.time() - started) * 1000), "BadUrl"
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        conn = pool.acquire(parts.scheme, host, port)
        reuse = False
        try:
            conn.request("HEAD", target, headers={"User-Agent": UA, "Accept": "text/html", "Connection": "keep-alive"})
            resp = conn.getresponse()
            resp.read()
            reuse = resp.will_close is False
            status = int(resp.status)
            location = resp.getheader("Location")
        except Exception as exc:
            return None, current, int((time.time() - started) * 1000), type(exc).__name__
        finally:
            pool.release(parts.scheme, host, port, conn, reuse)
        if status in (301, 302, 303, 307, 308) and location:
            current = resolve_location(current, location.strip())
            continue
        return status, current, int((time.time() - started) * 1000), ""
    return None, current, int((time.time() - started) * 1000), "TooManyRedirects"


class SiteLimiter:
    """Per-site concurrency that backs off on throttle responses."""

    def __init__(self, sites, workers):
        self.limit = {site: max(1, workers) for site in sites}
        self.inflight = {site: 0 for site in sites}
        self.throttle_streak = Counter()
        self._lock = threading.Condition()

    def acquire(self, site):
        with self._lock:
            while self.inflight[site] >= self.limit[site]:
                self._lock.wait(0.05)
            self.inflight[site] += 1

    def release(self, site, status):
        with self._lock:
            self.inflight[site] = max(0, self.inflight[site] - 1)
            if status in THROTTLE:
                self.throttle_streak[site] += 1
                if self.throttle_streak[site] >= 2 and self.limit[site] > 1:
                    self.limit[site] = max(1, self.limit[site] // 2)
                    self.throttle_streak[site] = 0
                    print("throttle {} concurrency={}".format(site, self.limit[site]), file=sys.stderr, flush=True)
            elif status is not None:
                self.throttle_streak[site] = 0
                if self.limit[site] < 32 and status and 200 <= status < 400:
                    self.limit[site] += 0  # success does not immediately ramp; the next pass starts higher
            self._lock.notify_all()


def sample_urls(path, quotas, seed):
    """Reservoir-sample up to quotas[site] URLs. A quota of 0 keeps every URL of that site."""
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
            quota = quotas.get(website, 0)
            bucket = buckets[website]
            if quota <= 0 or len(bucket) < quota:
                bucket.append(url)
            else:
                j = rng.randrange(seen[website])
                if j < quota:
                    bucket[j] = url
            if rows % 5000000 == 0:
                elapsed = max(time.time() - started, 0.001)
                print(
                    "sample {:,} rows  {:.1f}%  {:.0f} MiB/s".format(rows, 100.0 * read / size, (read / 1048576) / elapsed),
                    file=sys.stderr,
                    flush=True,
                )
    return dict(seen), {site: list(urls) for site, urls in buckets.items()}


def count_population(path):
    seen = Counter()
    with open(path, "rb") as fh:
        fh.readline()
        for raw in fh:
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw:
                continue
            parts = raw.decode("utf-8", errors="replace").split(DELIM)
            if len(parts) == 6 and parts[1].strip().startswith("http"):
                seen[parts[0]] += 1
    return dict(seen)


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


def pilot_from(done):
    counts = defaultdict(lambda: [0, 0])
    for (site, _url), row in done.items():
        verdict = row.get("verdict")
        if verdict == "alive":
            counts[site][0] += 1
        elif verdict == "dead":
            counts[site][1] += 1
    return {site: tuple(pair) for site, pair in counts.items()}


def check_all(jobs, results_path, workers, timeout, connections, margin, full):
    """Check URLs until each site is decided within `margin`, or the list runs out.

    `jobs` is a dict of site -> [url]. Already decided rows are skipped. Unknown
    rows are eligible again. Throttle status lowers that site's concurrency.
    """
    done = load_done(results_path)
    pending = {}
    for site, urls in jobs.items():
        waiting = []
        for url in urls:
            prev = done.get((site, url))
            if prev is None or prev.get("verdict") == "unknown":
                waiting.append(url)
        pending[site] = waiting
    total = sum(len(v) for v in pending.values())
    print("pending {:,}".format(total), file=sys.stderr, flush=True)
    if total == 0:
        return done

    pool = ConnectionPool(connections, timeout)
    per_site_workers = max(1, workers // max(1, len(pending)))
    limiter = SiteLimiter(list(pending), per_site_workers)
    stop = set()
    finished = 0
    cursors = {site: 0 for site in pending}

    def site_done(site):
        if full or margin <= 0:
            return False
        alive = dead = 0
        for url in jobs.get(site, []):
            verdict = done.get((site, url), {}).get("verdict")
            if verdict == "alive":
                alive += 1
            elif verdict == "dead":
                dead += 1
        interval = wilson(alive, alive + dead)
        if interval is None:
            return False
        _rate, low, high = interval
        return (high - low) <= margin and (alive + dead) >= 30

    def work(site, url):
        limiter.acquire(site)
        try:
            status, final, ms, error = request_once(url, pool)
        finally:
            limiter.release(site, status)
        return {
            "site": site,
            "url": url,
            "verdict": classify(status, final),
            "status": status,
            "final": final,
            "ms": ms,
            "error": error,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    try:
        with open(results_path, "a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=workers) as executor:
            while True:
                wave = []
                for site, urls in pending.items():
                    if site in stop:
                        continue
                    room = max(1, limiter.limit[site])
                    for _ in range(room):
                        if cursors[site] >= len(urls):
                            break
                        wave.append((site, urls[cursors[site]]))
                        cursors[site] += 1
                if not wave:
                    break
                futures = [executor.submit(work, site, url) for site, url in wave]
                for fut in as_completed(futures):
                    row = fut.result()
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    done[(row["site"], row["url"])] = row
                    finished += 1
                    if finished % 50 == 0:
                        print("checked {:,}".format(finished), file=sys.stderr, flush=True)
                for site in pending:
                    if site not in stop and site_done(site):
                        stop.add(site)
                        print("stop {} interval within {:.1%}".format(site, margin), file=sys.stderr, flush=True)
    finally:
        pool.close()
    print("checked {:,}".format(finished), file=sys.stderr, flush=True)
    return done


def write_report(out_dir, population, done):
    by_site = defaultdict(Counter)
    for (site, _url), row in done.items():
        if site in population:
            by_site[site][row.get("verdict") or "unknown"] += 1

    lines = [
        "# Liveness report",
        "",
        "alive = the final page returned HTTP 200-299 and is not the site homepage.",
        "dead = HTTP 404 or 410, or a redirect that ends on the homepage. A removed video often redirects there.",
        "unknown = timeout, connection error, 403, 429 or 503. Unknown is not counted as dead.",
        "Sample sizes follow Neyman allocation (more checks where the alive rate is least certain).",
        "A site stops early once its 95% Wilson interval is narrower than the requested margin.",
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
        est = lo = hi = None
        if interval is None:
            rate_text, est_text, band = "-", "-", "-"
        else:
            any_estimate = True
            rate, lo_r, hi_r = interval
            est, lo, hi = pop * rate, pop * lo_r, pop * hi_r
            total_est += est
            total_lo += lo
            total_hi += hi
            rate_text = "{:.1%}".format(rate)
            est_text = "{:,.0f}".format(est)
            band = "{:,.0f} - {:,.0f}".format(lo, hi)
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
        lines.append("Estimated live URLs: **{:,.0f}** of {:,} ({:.1%}).".format(total_est, total_pop, total_est / total_pop))
        lines.append("Sum of the per-site 95% intervals: {:,.0f} - {:,.0f}.".format(total_lo, total_hi))
    else:
        lines.append("No decided rows yet, so there is no estimate.")
    lines.append("")
    lines.append("The rate uses alive / (alive + dead). Unknowns are excluded, not treated as dead.")
    text_out = "\n".join(lines) + "\n"
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(text_out)
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
    return text_out


def load_or_sample(args):
    os.makedirs(args.out, exist_ok=True)
    sample_path = os.path.join(args.out, "sample.json")
    results_path = os.path.join(args.out, "results.jsonl")
    if args.per_site == 0:
        quotas = None
    else:
        if os.path.exists(sample_path):
            saved = json.load(open(sample_path, encoding="utf-8"))
            population = saved["population"]
        else:
            print("counting rows", file=sys.stderr, flush=True)
            population = count_population(args.input)
        pilot = pilot_from(load_done(results_path))
        quotas = allocate(population, args.per_site, pilot, floor=args.floor)
        print("allocation " + json.dumps(quotas, ensure_ascii=False), file=sys.stderr, flush=True)

    if os.path.exists(sample_path) and quotas is not None:
        saved = json.load(open(sample_path, encoding="utf-8"))
        same = saved.get("quotas") == quotas and saved.get("seed") == args.seed
        if same:
            print("reusing {}".format(sample_path), file=sys.stderr)
            return saved["population"], saved["samples"]

    if quotas is None:
        quotas = {}
    population, samples = sample_urls(args.input, quotas, args.seed)
    with open(sample_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"population": population, "samples": samples, "quotas": quotas, "per_site": args.per_site, "seed": args.seed},
            fh,
            ensure_ascii=False,
        )
    print("wrote {}".format(sample_path), file=sys.stderr)
    return population, samples


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="Path to the interrobang-delimited file")
    parser.add_argument("--out", default="output/liveness", help="Directory for sample, results and report")
    parser.add_argument("--per-site", type=int, default=1000, help="Total sample budget across websites (0 = every URL)")
    parser.add_argument("--margin", type=float, default=0.03, help="Stop a website once its 95%% interval is this narrow")
    parser.add_argument("--floor", type=int, default=40, help="Minimum checks for a website before allocation favors the uncertain ones")
    parser.add_argument("--seed", type=int, default=20261016)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--connections", type=int, default=4, help="Keep-alive connections kept per origin")
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--passes", type=int, default=2, help="Passes over rows that are still unknown")
    args = parser.parse_args()

    population, samples = load_or_sample(args)
    results_path = os.path.join(args.out, "results.jsonl")
    done = {}
    for _ in range(max(1, args.passes)):
        done = check_all(
            samples,
            results_path,
            args.workers,
            args.timeout,
            args.connections,
            args.margin,
            full=args.per_site == 0,
        )
        unknown = sum(1 for row in done.values() if row.get("verdict") == "unknown")
        print("unknown remaining {:,}".format(unknown), file=sys.stderr, flush=True)
        if unknown == 0:
            break
    sys.stdout.write(write_report(args.out, population, done))


if __name__ == "__main__":
    main()
