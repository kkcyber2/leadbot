# leadbot

**A free, laptop-free lead-generation pipeline that runs entirely on GitHub Actions.**

Two pipelines in one repo: it finds **contract-shaped job leads** from public job APIs and RSS feeds, and **local businesses with no website, a broken website, or an outdated one** — then scores them against a configurable skill profile and sends a daily digest to Telegram.

No server. No VPS. No laptop. No paid API. Standard library only.

[![tests](https://github.com/kkcyber2/leadbot/actions/workflows/tests.yml/badge.svg)](https://github.com/kkcyber2/leadbot/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/runtime%20deps-none-brightgreen)](requirements.txt)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

---

## Why this exists

Most "lead generation automation" advice assumes you have a machine to run it on and money to spend on APIs. This was built for the opposite constraint: **a phone, a $0 budget, and no laptop.**

That constraint drives every design decision:

| Constraint | Decision |
|---|---|
| No machine | GitHub Actions cron — runs in GitHub's cloud |
| $0 budget | Only free, no-key APIs and public RSS |
| Phone-only editing | All tuning lives in `leads.config.json`, not code |
| Must not break silently | Failure alarm step + committed state file |
| Must be honest | Every lead is tagged `contract`/`fulltime`/`unknown` |

---

## What it actually does

```
                    ┌─────────────────────────────┐
                    │   GitHub Actions (cron)     │
                    │   06:00 UTC daily           │
                    └──────────────┬──────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              │                                         │
     ┌────────▼─────────┐                     ┌─────────▼────────┐
     │  leadbot.py      │                     │  localbot.py     │
     │  job boards      │                     │  OSM businesses  │
     └────────┬─────────┘                     └─────────┬────────┘
              │                                         │
   RemoteOK · Remotive · WWR · Jobicy        Overpass API (free, no key)
   Himalayas · Arbeitnow · HN · Reddit       → HTTP-audit each website
              │                                         │
              └────────────────────┬────────────────────┘
                                   │
                        ┌──────────▼──────────┐
                        │  score vs. profile  │
                        │  leads.config.json  │
                        └──────────┬──────────┘
                                   │
                        ┌──────────▼──────────┐
                        │  Telegram digest    │
                        │  + Sheets (optional)│
                        └─────────────────────┘
```

### The job pipeline

Pulls from eight free sources, normalises everything to one shape, then applies a **title gate** before scoring:

- The **title** must contain a technical term (`engineer`, `security`, `devops`, `llm`, …) and must not contain a non-technical one (`account executive`, `recruiter`, `nurse`, …).
- Body text is deliberately **not** used for the gate — every job description name-drops a stack, so a body-based filter lets "Enterprise Account Executive (we use Python!)" through.
- Seller posts (`[For Hire]`, `Hire me`) are dropped. On r/forhire only `[Hiring]` posts survive.
- Anything older than `max_age_days` is dropped.

Then it scores against nine skill categories, each with its own weight, term cap, and an `enabled` switch.

### The local-business pipeline

Queries OpenStreetMap's Overpass API for local trades in ten cities, drops chains, then **HTTP-audits each business's website** and scores the defect:

| Signal | What it means |
|---|---|
| `no_website` | OSM has no `website` tag — strongest signal |
| `broken` | DNS failure, TLS error, or 4xx/5xx |
| `no_https` | Serves over plain HTTP |
| `no_viewport` | No mobile viewport meta — unreadable on a phone |
| `stale_year` | Footer copyright ≥ 2 years old |
| `builder_site` | Wix / Squarespace / WordPress detected |
| `slow` | TTFB over `slow_ms` |
| `thin_content` | Almost no body text |

A business whose site is fine scores **0** and never appears — there is nobody to sell to.

---

## Quick start

### 1. Fork and clone

```bash
git clone https://github.com/kkcyber2/leadbot.git
cd leadbot
```

### 2. Try it without sending anything

```bash
python3 leadbot.py --dry-run          # job pipeline, prints, sends nothing
python3 localbot.py --dry-run         # local business pipeline
python3 leadbot.py --dry-run --source remoteok   # one source, for debugging
```

`--dry-run` never sends, never writes state, never needs a token.

### 3. Run the tests

```bash
pip install -r requirements.txt
python3 -m pytest -q
```

115 tests, no network access required — every source is mocked or fixture-driven.

### 4. Wire up the digest

Add these as **repo Secrets** (Settings → Secrets and variables → Actions):

| Secret | Required | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | for digest | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | for digest | Your chat id — **press Start on the bot first** |
| `SHEETS_WEBHOOK_URL` | optional | Apps Script web app URL |

Without the Telegram secrets the run still works — it just prints to the log instead of sending.

### 5. Turn it on

The cron is already in `.github/workflows/leadbot.yml`. Push to `main` and it runs at 06:00 UTC. Use **Actions → leadbot → Run workflow** to trigger one manually.

---

## Tuning it from your phone

Everything lives in `leads.config.json`. Open it on github.com, tap the pencil, edit, commit. No Python needed.

### Turn a skill category off

```json
"gamedev": { "enabled": false, "weight": 3, "cap": 2, "terms": ["unity", "unreal"] }
```

`enabled: false` stops it scoring immediately. Nothing is deleted, so flipping it back is one word.

### Change what counts as a technical role

```json
"must_match_title": ["engineer", "developer", "security", "llm"],
"block_title_terms": ["account executive", "recruiter", "nurse"]
```

A title must match one allow-term and zero block-terms. The two lists must not overlap — a test enforces that.

### Add a city

```json
{ "name": "Berlin", "country": "Germany", "lat": 52.52, "lon": 13.405,
  "size_km": 6, "currency": "EUR", "priority": 11 }
```

`size_km` is the **half-width** of the search box, so `6` means a 12 km box.

### Add a trade

```json
{ "tag": "shop", "value": "optician", "label": "optician", "budget": "high" }
```

`tag`/`value` are the raw OSM keys. `budget` is `low`/`mid`/`high` and feeds the score.

---

## Repo layout

```
leadbot/
├── leadbot.py                    # job pipeline (886 lines)
├── localbot.py                   # local business pipeline (689 lines)
├── leads.config.json             # all tuning — edit from your phone
├── requirements.txt              # pytest only; runtime deps are zero
├── LICENSE                       # MIT
├── .gitignore
├── .github/workflows/
│   ├── leadbot.yml               # the daily cron
│   └── tests.yml                 # pytest on 3.9 / 3.11 / 3.12
├── apps-script/
│   └── sheets-bridge.gs          # Google Sheets webhook
└── tests/
    ├── conftest.py
    ├── test_leadbot.py           # 53 tests
    └── test_localbot.py          # 62 tests
```

---

## Design notes

**Standard library only.** No `requests`, no `feedparser`, no `beautifulsoup`. This is deliberate: the Actions job needs no install step, so it cannot break on a dependency bump, and it runs on any Python 3.9+ machine with zero setup.

**The title gate exists because body-text filtering does not work.** The first version scored body text only and shipped four competitors' `[For Hire]` posts in the digest. The gate is the fix, and `test_reject_drops_seller_posts` pins it.

**Dates are parsed three ways.** RSS gives RFC-822 (`Mon, 27 Oct 2026 10:00:00 +0000`), Atom gives ISO-8601, some feeds give a bare date. An early version only handled ISO, so every RSS lead was silently marked "undated" and skipped the staleness check.

**Chains are dropped by name, not by size.** A missing `website` tag on a Pizza Hut means nobody filled the tag in, not that they need a site. `block_brands` is a plain list you can extend.

**A fine website scores zero.** The local pipeline is a defect finder, not a directory. If the site is modern, mobile-ready, HTTPS, and current, there is no pitch.

**State is committed, not cached.** `seen.json` is written back to the repo so the same lead never appears twice. That is why the workflow needs `contents: write`.

---

## Honest limitations

- **Overpass is slow and flaky.** Public instances 502 under load. The code rotates mirrors and retries, but a run can still come back thin. It is free; that is the trade.
- **OSM coverage is uneven.** Dubai and London are well mapped; smaller cities are not. A missing business is usually a missing OSM record, not a missing business.
- **`no_website` is a proxy.** Some businesses have a site that simply is not tagged. The pitch is written to survive that ("I couldn't find a site for you — if I missed it, ignore this").
- **Hunter.io is off by default.** Email enrichment costs credits; the free tier is 50/month. Turn it on only after the scoring is tuned.
- **No reply tracking.** This finds and drafts. Sending and following up is still manual, on purpose.

---

## License

MIT — see [LICENSE](LICENSE).

Data from OpenStreetMap is © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).
