#!/usr/bin/env python3
"""Stream-profile a large interrobang-delimited metadata file.

Expected header:
    website‽url‽title‽tags‽upload_date‽video_id

The profiler never loads the file into memory. Duplicate detection uses an
on-disk set of "website\0video_id" keys.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import sys
import time
from typing import Dict, Optional, TextIO, Tuple


DELIM = "\u203d"  # ‽
COLUMNS = ("website", "url", "title", "tags", "upload_date", "video_id")
DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y")


def parse_date(raw: str) -> Optional[dt.date]:
    text = raw.strip()
    if not text:
        return None
    # Fast path: DD/MM/YYYY, which dominates this dump.
    if len(text) == 10 and text[2] == "/" and text[5] == "/":
        try:
            return dt.date(int(text[6:10]), int(text[3:5]), int(text[0:2]))
        except ValueError:
            pass
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class SampledIdentity:
    """Exact duplicate counts on a deterministic sample of identity keys.

    A full exact set of tens of millions of `website + video_id` keys does
    not fit comfortably in a ~16 GB machine next to the OS. Hash the key and
    keep it only when `hash(key) % sample_mod == 0`. The duplicate *rate*
    inside that slice estimates the rate in the full file; raw counts are
    reported both as sample counts and as a full-file estimate.
    """

    def __init__(self, sample_mod: int) -> None:
        if sample_mod < 1:
            raise ValueError("sample_mod must be >= 1")
        self.sample_mod = sample_mod
        self._seen = set()
        self.sampled = 0
        self.duplicates = 0
        self.unique = 0
        self.empty = 0
        self.considered = 0

    def add(self, key: str) -> None:
        self.considered += 1
        if not key or key.endswith("\0"):
            self.empty += 1
            return
        if (hash(key) % self.sample_mod) != 0:
            return
        self.sampled += 1
        if key in self._seen:
            self.duplicates += 1
            return
        self._seen.add(key)
        self.unique += 1


def probe(path: str, out_dir: str, top_n: int, progress_every: int, sample_mod: int) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    size = os.path.getsize(path)
    started = time.time()

    websites: Dict[str, int] = collections.Counter()
    tags: Dict[str, int] = collections.Counter()
    years: Dict[str, int] = collections.Counter()
    tag_counts_hist: Dict[str, int] = collections.Counter()
    field_empty = collections.Counter()
    malformed = collections.Counter()

    rows = 0
    data_rows = 0
    title_len_sum = 0
    title_len_max = 0
    url_len_sum = 0
    url_len_max = 0
    min_date: Optional[dt.date] = None
    max_date: Optional[dt.date] = None
    dates_ok = 0
    dates_missing = 0
    dates_bad = 0
    bytes_read = 0

    keys = SampledIdentity(sample_mod)

    with open(path, "rb") as fh:
        header = fh.readline()
        bytes_read += len(header)
        header_text = header.decode("utf-8-sig", errors="replace").rstrip("\r\n")
        header_cols = header_text.split(DELIM)
        if tuple(header_cols) != COLUMNS:
            raise SystemExit(
                "unexpected header: {0!r}\nexpected: {1}".format(
                    header_text, DELIM.join(COLUMNS)
                )
            )

        for raw in fh:
            bytes_read += len(raw)
            rows += 1
            # Keep the newline strip in bytes to avoid a second decode pass cost
            # on the line ending only.
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw:
                malformed["empty_line"] += 1
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                malformed["non_utf8"] += 1
                line = raw.decode("utf-8", errors="replace")

            parts = line.split(DELIM)
            if len(parts) != 6:
                malformed["width_%d" % len(parts)] += 1
                continue

            data_rows += 1
            website, url, title, tag_field, upload_date, video_id = parts
            websites[website] += 1

            for name, value in (
                ("website", website),
                ("url", url),
                ("title", title),
                ("tags", tag_field),
                ("upload_date", upload_date),
                ("video_id", video_id),
            ):
                if not value.strip():
                    field_empty[name] += 1

            tlen = len(title)
            ulen = len(url)
            title_len_sum += tlen
            url_len_sum += ulen
            if tlen > title_len_max:
                title_len_max = tlen
            if ulen > url_len_max:
                url_len_max = ulen

            parsed_tags = [t.strip() for t in tag_field.split(",") if t.strip()]
            tag_counts_hist[str(len(parsed_tags))] += 1
            for tag in parsed_tags:
                tags[tag] += 1

            if not upload_date.strip():
                dates_missing += 1
            else:
                parsed = parse_date(upload_date)
                if parsed is None:
                    dates_bad += 1
                else:
                    dates_ok += 1
                    years[str(parsed.year)] += 1
                    if min_date is None or parsed < min_date:
                        min_date = parsed
                    if max_date is None or parsed > max_date:
                        max_date = parsed

            keys.add(website + "\0" + video_id.strip())

            if progress_every and rows % progress_every == 0:
                elapsed = time.time() - started
                mb = bytes_read / (1024 * 1024)
                rate = mb / elapsed if elapsed else 0
                pct = 100.0 * bytes_read / size if size else 0
                print(
                    "rows={:,}  {:.1f}%  {:.0f} MiB/s  dup={:,}".format(
                        rows, pct, rate, keys.duplicates
                    ),
                    file=sys.stderr,
                    flush=True,
                )

    elapsed = time.time() - started
    summary = {
        "input": os.path.abspath(path),
        "bytes": size,
        "elapsed_seconds": round(elapsed, 2),
        "throughput_mib_per_s": round((size / (1024 * 1024)) / elapsed, 2) if elapsed else None,
        "physical_lines_after_header": rows,
        "well_formed_rows": data_rows,
        "malformed": dict(malformed),
        "empty_fields": dict(field_empty),
        "websites": websites.most_common(),
        "unique_tags": len(tags),
        "top_tags": tags.most_common(top_n),
        "tags_per_row": dict(sorted(tag_counts_hist.items(), key=lambda kv: int(kv[0]))),
        "title_len": {
            "mean": round(title_len_sum / data_rows, 2) if data_rows else None,
            "max": title_len_max,
        },
        "url_len": {
            "mean": round(url_len_sum / data_rows, 2) if data_rows else None,
            "max": url_len_max,
        },
        "upload_date": {
            "parsed": dates_ok,
            "missing": dates_missing,
            "unparseable": dates_bad,
            "min": min_date.isoformat() if min_date else None,
            "max": max_date.isoformat() if max_date else None,
            "by_year": dict(sorted(years.items())),
        },
        "identity": {
            "method": "deterministic hash sample, exact within the sample",
            "sample_modulus": keys.sample_mod,
            "keys_considered": keys.considered,
            "missing_video_id": keys.empty,
            "sampled_keys": keys.sampled,
            "sampled_unique": keys.unique,
            "sampled_duplicate_hits": keys.duplicates,
            "sample_duplicate_rate": round(keys.duplicates / keys.sampled, 6) if keys.sampled else None,
            "estimated_duplicate_hits_full": keys.duplicates * keys.sample_mod,
        },
    }

    json_path = os.path.join(out_dir, "summary.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    md_path = os.path.join(out_dir, "report.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        write_report(fh, summary, top_n)

    print("wrote %s" % json_path, file=sys.stderr)
    print("wrote %s" % md_path, file=sys.stderr)
    return summary


def fmt_int(n: int) -> str:
    return "{:,}".format(n)


def write_report(fh: TextIO, s: dict, top_n: int) -> None:
    rows = s["well_formed_rows"] or 1
    fh.write("# Data probe report\n\n")
    fh.write("- Input: `%s`\n" % s["input"])
    fh.write("- Size: %s bytes (%.2f GiB)\n" % (fmt_int(s["bytes"]), s["bytes"] / (1024 ** 3)))
    fh.write("- Elapsed: %.1f s (%.1f MiB/s)\n" % (s["elapsed_seconds"], s["throughput_mib_per_s"] or 0))
    fh.write("- Well-formed rows: %s\n" % fmt_int(s["well_formed_rows"]))
    fh.write("- Physical lines after header: %s\n" % fmt_int(s["physical_lines_after_header"]))
    fh.write("\nField separator is U+203D `‽`, not a comma. Columns: %s.\n" % ", ".join(COLUMNS))

    fh.write("\n## Malformed rows\n\n")
    if not s["malformed"]:
        fh.write("None.\n")
    else:
        for k, v in sorted(s["malformed"].items(), key=lambda kv: -kv[1]):
            fh.write("- %s: %s\n" % (k, fmt_int(v)))

    fh.write("\n## Empty fields\n\n")
    fh.write("| field | empty | share |\n|---|---:|---:|\n")
    for name in COLUMNS:
        n = s["empty_fields"].get(name, 0)
        fh.write("| %s | %s | %.2f%% |\n" % (name, fmt_int(n), 100.0 * n / rows))

    fh.write("\n## Websites\n\n")
    fh.write("| website | rows | share |\n|---|---:|---:|\n")
    for name, n in s["websites"]:
        fh.write("| %s | %s | %.2f%% |\n" % (name or "(blank)", fmt_int(n), 100.0 * n / rows))

    ident = s["identity"]
    fh.write("\n## Identity\n\n")
    fh.write(
        "Duplicate detection is exact inside a 1/%d deterministic sample of "
        "`(website, video_id)` keys. The full-file duplicate count is estimated "
        "by scaling that sample.\n\n" % ident["sample_modulus"]
    )
    fh.write("- Keys considered: %s\n" % fmt_int(ident["keys_considered"]))
    fh.write("- Missing video_id: %s\n" % fmt_int(ident["missing_video_id"]))
    fh.write("- Sampled keys: %s\n" % fmt_int(ident["sampled_keys"]))
    fh.write("- Unique inside sample: %s\n" % fmt_int(ident["sampled_unique"]))
    fh.write("- Duplicate hits inside sample: %s\n" % fmt_int(ident["sampled_duplicate_hits"]))
    fh.write("- Sample duplicate rate: %s\n" % ident["sample_duplicate_rate"])
    fh.write("- Estimated duplicate hits in the full file: %s\n" % fmt_int(ident["estimated_duplicate_hits_full"]))

    fh.write("\n## Text length\n\n")
    fh.write("- Title length mean / max: %s / %s\n" % (s["title_len"]["mean"], s["title_len"]["max"]))
    fh.write("- URL length mean / max: %s / %s\n" % (s["url_len"]["mean"], s["url_len"]["max"]))

    ud = s["upload_date"]
    fh.write("\n## Upload date\n\n")
    fh.write("- Parsed: %s\n" % fmt_int(ud["parsed"]))
    fh.write("- Missing: %s\n" % fmt_int(ud["missing"]))
    fh.write("- Unparseable: %s\n" % fmt_int(ud["unparseable"]))
    fh.write("- Min / max: %s / %s\n" % (ud["min"], ud["max"]))
    fh.write("\n| year | rows |\n|---|---:|\n")
    for year, n in ud["by_year"].items():
        fh.write("| %s | %s |\n" % (year, fmt_int(n)))

    fh.write("\n## Tags\n\n")
    fh.write("- Unique tags: %s\n" % fmt_int(s["unique_tags"]))
    fh.write("\nTags per row:\n\n")
    fh.write("| tags | rows |\n|---:|---:|\n")
    for k, n in s["tags_per_row"].items():
        fh.write("| %s | %s |\n" % (k, fmt_int(n)))
    fh.write("\nTop %d tags:\n\n" % top_n)
    fh.write("| tag | rows | share |\n|---|---:|---:|\n")
    for name, n in s["top_tags"]:
        fh.write("| %s | %s | %.2f%% |\n" % (name.replace("|", "\\|"), fmt_int(n), 100.0 * n / rows))
    fh.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to the ‽-delimited file")
    parser.add_argument("--out", default="output", help="Directory for report.md and summary.json")
    parser.add_argument("--top", type=int, default=40, help="How many tags to keep")
    parser.add_argument("--progress-every", type=int, default=2_000_000)
    parser.add_argument(
        "--dup-sample",
        type=int,
        default=50,
        help="Keep 1/N of identity keys for exact duplicate detection (1 = full set)",
    )
    args = parser.parse_args()
    probe(args.input, args.out, args.top, args.progress_every, args.dup_sample)


if __name__ == "__main__":
    main()
