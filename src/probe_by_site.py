import collections, datetime as dt, json, os, sys, time

DELIM = "\u203d"
COLUMNS = ("website", "url", "title", "tags", "upload_date", "video_id")
FMTS = ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y")

def parse_date(text):
    text = text.strip()
    if not text:
        return None
    if len(text) == 10 and text[2] == "/" and text[5] == "/":
        try:
            return dt.date(int(text[6:10]), int(text[3:5]), int(text[0:2]))
        except ValueError:
            pass
    for fmt in FMTS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

path = sys.argv[1]
out = sys.argv[2]
sites = collections.defaultdict(lambda: {
    "rows": 0,
    "empty": collections.Counter(),
    "dates_ok": 0,
    "dates_missing": 0,
    "dates_bad": 0,
    "min": None,
    "max": None,
    "years": collections.Counter(),
    "tag_rows": 0,
    "tag_sum": 0,
    "title_sum": 0,
    "title_max": 0,
    "url_sum": 0,
    "url_max": 0,
})
started = time.time()
n = 0
size = os.path.getsize(path)
read = 0
with open(path, "rb") as fh:
    header = fh.readline()
    read += len(header)
    for raw in fh:
        read += len(raw)
        n += 1
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        line = raw.decode("utf-8", errors="replace")
        parts = line.split(DELIM)
        if len(parts) != 6:
            continue
        website, url, title, tag_field, upload_date, video_id = parts
        s = sites[website]
        s["rows"] += 1
        for name, value in (("title", title), ("tags", tag_field), ("upload_date", upload_date), ("url", url), ("video_id", video_id)):
            if not value.strip():
                s["empty"][name] += 1
        tl, ul = len(title), len(url)
        s["title_sum"] += tl
        s["url_sum"] += ul
        if tl > s["title_max"]:
            s["title_max"] = tl
        if ul > s["url_max"]:
            s["url_max"] = ul
        tags = [t.strip() for t in tag_field.split(",") if t.strip()]
        s["tag_rows"] += 1
        s["tag_sum"] += len(tags)
        if not upload_date.strip():
            s["dates_missing"] += 1
        else:
            parsed = parse_date(upload_date)
            if parsed is None:
                s["dates_bad"] += 1
            else:
                s["dates_ok"] += 1
                s["years"][parsed.year] += 1
                if s["min"] is None or parsed < s["min"]:
                    s["min"] = parsed
                if s["max"] is None or parsed > s["max"]:
                    s["max"] = parsed
        if n % 5000000 == 0:
            el = time.time() - started
            print("rows=%d %.1f%% %.0fMiB/s" % (n, 100.0*read/size, (read/1048576)/el if el else 0), file=sys.stderr, flush=True)

result = {}
for name, s in sites.items():
    rows = s["rows"] or 1
    result[name] = {
        "rows": s["rows"],
        "empty": dict(s["empty"]),
        "dates_ok": s["dates_ok"],
        "dates_missing": s["dates_missing"],
        "dates_bad": s["dates_bad"],
        "min": s["min"].isoformat() if s["min"] else None,
        "max": s["max"].isoformat() if s["max"] else None,
        "mean_tags": round(s["tag_sum"] / rows, 2),
        "title_mean": round(s["title_sum"] / rows, 2),
        "title_max": s["title_max"],
        "url_mean": round(s["url_sum"] / rows, 2),
        "url_max": s["url_max"],
        "years": {str(k): v for k, v in sorted(s["years"].items())},
    }
with open(out, "w", encoding="utf-8") as fh:
    json.dump(result, fh, ensure_ascii=False, indent=2)
print("wrote", out, "in", round(time.time()-started,1), "s", file=sys.stderr)
