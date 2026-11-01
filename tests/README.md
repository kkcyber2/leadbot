> **Note:** every file in `tests/` is authored to run **offline**. No test
> makes a network call, so CI is deterministic and cannot flake on a dead feed.

# tests

Run them:

```bash
pip install -r requirements.txt
python -m pytest -q
```

115 tests across two files, 0 network calls, ~0.4 s.

## `test_leadbot.py` — the job pipeline (60 tests)

| Group | What it pins down |
|---|---|
| text helpers | `clean` strips tags and unescapes entities; `clip` measures after cleaning |
| dedupe keys | `norm_key` is stable, case/punctuation-insensitive, company-sensitive |
| feed parsing | RSS **and** Atom; CDATA bodies; items without a link dropped; garbage returns `[]` |
| rejection gate | seller posts, missing hiring tag, missing URL, thin content, non-technical titles, content series, staleness, config `block_terms` |
| engagement | contract vs full-time vs unknown, and the tie-break rule |
| date parsing | RFC-822, ISO-with-Z, plain date, junk → `None` |
| scoring | security/AI terms rewarded, negatives penalised, freshness bonus, contract beats full-time, ≤6 matched terms |
| drafts | category template used, fallback to general, missing company handled, every category has a template |
| config sanity | 9 categories, 3 off by default, title gate populated, **no overlap between allow and block lists** |

The overlap test is the interesting one: a term in both `must_match_title` and
`block_title_terms` makes the gate order-dependent, so behaviour becomes an
accident rather than a rule.

## `test_localbot.py` — the local-business pipeline (55 tests)

| Group | What it pins down |
|---|---|
| bbox | centred; span matches the requested size; widens toward the poles; survives `cos(90°)=0` |
| normalise | trade mapping, unnamed/1-char rejection, `name:en` fallback, `contact:*` tags, social list, address, OSM attribution |
| chain filter | catches Pizza Hut / Nando's / KFC; allows independents; honours config `block_brands`; **`Fabrication Studio` is not `fab `** |
| biz_key | stable, punctuation-insensitive, city-sensitive |
| audit (mocked) | no-URL → `none`; good site → `ok`; detects missing viewport, HTTP-only, stale copyright, Wix, WordPress; 404 / DNS / TLS → `broken`; adds a scheme when missing |
| describe_problem | severity order, year + age in the stale message, builder named, ms in the slow message |
| scoring | no-site and broken are the top signals; `ok` scores zero; uncontactable penalised; premium trade and tier-1 market rewarded; weights are config-driven |
| pitch | the right template per defect, business name always present, every `PITCH` key reachable |
| config sanity | markets/trades present and well-formed, weights cover every signal, Overpass endpoints are HTTPS |

A note on `test_score_ok_site_is_zero`: a business with a *fine* site must score
zero, because there is nothing to sell them. If that ever stops being true, the
digest will fill with businesses that have no problem to fix.

## Two behaviours deliberately pinned

These look odd but are the real, measured behaviour. They are asserted so a
future refactor cannot change them silently:

1. **`classify_engagement` ties go to `fulltime`.** It returns `contract` only
   when the contract-term count *exceeds* the full-time count. Testing found
   `contract`/`contractor` in passing text mislabelled permanent roles.
2. **`bbox(size_km)` is a half-width.** `dlat` is both subtracted from and
   added to the centre, so the box spans `2 × size_km`.

## Config fixtures

`cfg` is the whole `leads.config.json`. `lcfg` is its `local` sub-dict, which is
what `localbot`'s functions actually take. Using `cfg` with `localbot.score()`
would silently read an empty weight table and score everything zero — a test
bug that looks like a passing test.
