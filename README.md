# liveness-probe

Check which URLs in a large metadata dump are still live.

The dump is not comma-separated CSV. One record is one line, six fields, split on U+203D INTERROBANG (`‽`):

```
website‽url‽title‽tags‽upload_date‽video_id
```

Counting fields does not say whether a page still exists. This program requests `url`.

| verdict | meaning |
|---|---|
| alive | HEAD returned HTTP 200–399 |
| dead | HTTP 404 or 410 |
| unknown | timeout, connection error, 403, 429, 503 |

503 and 429 are throttling on these hosts, so they are not counted as dead. They stay unknown and the next run retries them.

## Run

Python 3.8 or newer. Standard library only.

```bash
python src/liveness.py --input D:/data/data.csv --out output/liveness --per-site 200 --workers 12 --passes 2
```

Stop it whenever you want. Run the same command again: it reuses `sample.json` and only requests URLs that are missing or still unknown.

`--per-site 0` checks every URL instead of sampling. For about 24 million URLs at roughly 15 requests per second, that is on the order of two weeks. The sample mode is what you use to get an estimate without waiting that out.

## Output

| file | contents |
|---|---|
| `output/liveness/sample.json` | the URLs chosen for this seed and sample size |
| `output/liveness/results.jsonl` | one JSON object per attempt; later attempts override earlier ones |
| `output/liveness/report.md` | alive / dead / unknown and the estimated live count |
| `output/liveness/summary.json` | the same numbers, machine readable |

The estimate for a site is `rows_in_file * alive / (alive + dead)`. Unknowns are left out of the rate. The report gives a 95% Wilson interval per site.

## Tests

```bash
python -m unittest test.test_liveness
```

The tests use a local HTTP server. They do not touch the network or the data file.
