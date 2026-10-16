# liveness-probe

Check which URLs in a large metadata dump are still live.

The dump is not comma-separated CSV. One record is one line, six fields, split on U+203D INTERROBANG (`‽`):

```
website‽url‽title‽tags‽upload_date‽video_id
```

The file scan is cheap. Each URL still needs its own request, so the probe does not spend requests evenly.

| mechanism | what it saves |
|---|---|
| Neyman allocation | sample size per site is proportional to rows × √(p(1−p)). A site that is almost all alive gets few checks |
| early stop | a site stops once its 95% Wilson interval is narrower than `--margin` |
| connection reuse | one TLS session per origin instead of a new handshake per URL |
| adaptive throttle | HTTP 429/503 cuts that site's concurrency. 404 and 410 are never retried |

| verdict | meaning |
|---|---|
| alive | final response is HTTP 200–299 and the final path is not the site root |
| dead | HTTP 404 or 410, or a redirect that lands on the homepage |
| unknown | timeout, connection error, 403, 429, 503 |

Unknown is not counted as dead. Run the same command again and only those rows are retried.

## Run

Python 3.8 or newer. Standard library only.

```bash
python src/liveness.py --input D:/data/data.csv --out output/liveness --per-site 1000 --margin 0.03
```

`--per-site` is the total sample budget across all websites, not the count per website. The first run counts the file, allocates that budget, then checks. A later run with the same budget reuses the sample.

`--per-site 0` checks every URL and disables the early stop. For about 24 million URLs that is still days of network time. Connection reuse and the throttle backoff are what make that shorter. They do not remove the need to request each URL.

## Output

| file | contents |
|---|---|
| `output/liveness/sample.json` | population, allocation, and the URLs drawn for this seed |
| `output/liveness/results.jsonl` | one JSON object per attempt; a later attempt overrides an earlier one |
| `output/liveness/report.md` | alive / dead / unknown and the estimated live count |
| `output/liveness/summary.json` | the same numbers, machine readable |

The estimate for a site is `rows_in_file * alive / (alive + dead)`. Unknowns are left out of the rate. The report gives a 95% Wilson interval per site.

## Tests

```bash
python -m unittest discover -s test
```

The tests use a local HTTP server. They do not touch the network or the data file.
