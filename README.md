# data-probe

Streaming profiler for a large metadata dump that is **not** a normal CSV.

Records are separated by U+203D `‽` (interrobang):

```
website‽url‽title‽tags‽upload_date‽video_id
```

The profiler is one forward pass and stays near-constant in memory aside from
the website counter, the tag counter, and a sampled identity set. It does not
load the file, and this repository does not contain the source dump or any
credentials.

## Measures

- row count and malformed rows (wrong field count, empty line, non-UTF-8)
- empty-field rate per column
- rows per website
- tag frequency (`tags` is comma-separated) and tags-per-row
- title and URL length (mean, max)
- upload dates: missing, unparseable, min/max, rows per year
- duplicate `(website, video_id)` rate on a deterministic 1/N sample
  (`--dup-sample`, default 50). Exact within the sample; the full-file
  duplicate count is the sample count scaled by N.

## Run

```bash
python src/probe.py --input /path/to/data.csv --out output --top 40 --dup-sample 50
```

Python 3.8+, standard library only.

`output/` is gitignored. Copy `output/report.md` into the repo only when
you want to publish the aggregate summary.
