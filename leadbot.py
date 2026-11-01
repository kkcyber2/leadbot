#!/usr/bin/env python3
"""
leadbot — free, laptop-free lead generation.
Runs on GitHub Actions. Standard library ONLY: no pip install, no build step.

    python3 leadbot.py --dry-run          # fetch + score, print, send nothing
    python3 leadbot.py --source remoteok  # one source, for debugging

Env (repo Secrets — never in code):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   -> Telegram digest
    SHEETS_WEBHOOK_URL, SHEETS_TOKEN       -> Google Sheets storage
    GITHUB_TOKEN                           -> raises the GitHub API rate limit

Findings that shaped this file (verified 2026-11-01):
  * Reddit .json returns 403 from datacenter IPs. Only .rss works, and it
    rate-limits hard — see src_reddit.
  * infosec-jobs.com/rss/ now serves an unrelated site; cybersecurityjobsite
    returns 403; RemoteOK's per-category .rss endpoints are 410. Do not re-add.
  * HN Algolia /search ranks by relevance and returns the 2020 "Who is hiring"
    thread as its top hit. Use /search_by_date.
  * RSS pubDate is RFC-822, which datetime.fromisoformat() cannot read. That
    bug silently marked every RSS lead "undated" and killed freshness scoring.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "leads.config.json"
STATE_PATH = ROOT / "state.json"
OUT_DIR = ROOT / "out"

UA = "kk-leadbot/2.0 (+personal job lead aggregation; github.com/kkcyber2)"
TIMEOUT = 25


# ─────────────────────────────── http ───────────────────────────────

def http_get(url: str, *, accept: str = "*/*", retries: int = 2) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(5 + attempt * 10)
                continue
            if 400 <= e.code < 500:
                raise
            time.sleep(2 + attempt * 3)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + attempt * 3)
    raise last  # type: ignore[misc]


def jget(url: str):
    return json.loads(http_get(url, accept="application/json").decode("utf-8", "replace"))


# ─────────────────────────── text helpers ───────────────────────────

TAG_RE = re.compile(r"<[^>]+>")


def clean(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", s))).strip()


def clip(s: str, n: int) -> str:
    s = clean(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def norm_key(title: str, company: str) -> str:
    basis = re.sub(r"[^a-z0-9]+", "", f"{title} {company}".lower())
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def title_key(title: str) -> str:
    """Second dedupe gate.

    jobicy and jobicy-rss publish the same role with different company
    strings, so the title+company key misses it — the digest showed
    "Ads Quality Rater – Thai" twice before this existed.
    """
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())[:60]


# ─────────────────────── dead-simple RSS reader ──────────────────────
# Regex, not ElementTree: real feeds carry undeclared dc:/media: prefixes and
# bare '&' that break every strict parser. We need five fields.

def _tag(block: str, *names: str) -> str:
    for n in names:
        m = re.search(rf"<{n}[^>]*>(.*?)</{n}>", block, re.S | re.I)
        if m:
            cdata = re.match(r"\s*<!\[CDATA\[(.*?)\]\]>\s*$", m.group(1), re.S)
            val = clean(cdata.group(1) if cdata else m.group(1))
            if val:
                return val
    return ""


def _atom_link(block: str) -> str:
    m = re.search(r'<link[^>]+href="([^"]+)"', block, re.I)
    return m.group(1) if m else ""


def parse_feed(raw: str) -> list[dict]:
    items = re.findall(r"<item\b[^>]*>(.*?)</item>", raw, re.S | re.I)
    if not items:
        items = re.findall(r"<entry\b[^>]*>(.*?)</entry>", raw, re.S | re.I)
    out = []
    for b in items:
        title = _tag(b, "title")
        link = _tag(b, "link") or _atom_link(b)
        if not link or not title:
            continue
        body = _tag(b, "content:encoded", "encoded", "description", "summary", "content")
        author = _tag(b, "dc:creator", "creator", "author")
        company, role = author, title
        m = re.match(r"^([^:]{2,60}):\s*(.+)$", title)
        if m and not author:
            company, role = m.group(1).strip(), m.group(2).strip()
        out.append({
            "title": role, "company": company, "url": link,
            "posted": _tag(b, "pubDate", "published", "updated", "dc:date"),
            "text": body[:1400], "extra": _tag(b, "category")[:200],
        })
    return out


def _rss(url: str, source: str) -> list[dict]:
    raw = http_get(url, accept="application/rss+xml, application/xml, text/xml")
    items = parse_feed(raw.decode("utf-8", "replace"))
    for it in items:
        it["source"] = source
    return items


# ─────────────────────────── sources ───────────────────────────

def src_remoteok(cfg) -> list[dict]:
    out = []
    for j in jget("https://remoteok.com/api"):
        if not isinstance(j, dict) or "position" not in j:
            continue  # element 0 is the legal notice
        out.append({
            "title": j.get("position", ""), "company": j.get("company", ""),
            "url": j.get("url") or j.get("apply_url") or "",
            "source": "remoteok", "posted": str(j.get("date", "")),
            "text": clean(j.get("description", ""))[:1400],
            "extra": " ".join(j.get("tags") or []),
        })
    return out


def src_remotive(cfg) -> list[dict]:
    out = []
    for cat in ("software-dev", "devops", "data"):
        try:
            d = jget(f"https://remotive.com/api/remote-jobs?category={cat}&limit=60")
        except Exception:  # noqa: BLE001
            continue
        for j in d.get("jobs", []):
            out.append({
                "title": j.get("title", ""), "company": j.get("company_name", ""),
                "url": j.get("url", ""), "source": "remotive",
                "posted": j.get("publication_date", ""),
                "text": clean(j.get("description", ""))[:1400],
                "extra": " ".join(j.get("tags") or []) + " "
                         + str(j.get("candidate_required_location", "")),
            })
    return out


def src_jobicy(cfg) -> list[dict]:
    out = []
    for ind in ("engineering", "technology", "dev"):
        try:
            d = jget(f"https://jobicy.com/api/v2/remote-jobs?count=50&industry={ind}")
        except Exception:  # noqa: BLE001
            continue
        for j in d.get("jobs", []):
            out.append({
                "title": j.get("jobTitle", ""), "company": j.get("companyName", ""),
                "url": j.get("url", ""), "source": "jobicy",
                "posted": j.get("pubDate", ""),
                "text": clean(j.get("jobDescription") or j.get("jobExcerpt", ""))[:1400],
                "extra": f"{j.get('jobGeo','')} {j.get('jobIndustry','')} {j.get('jobLevel','')}",
            })
    return out


def src_himalayas(cfg) -> list[dict]:
    out = []
    for q in ("security", "automation", "full-stack"):
        try:
            d = jget(f"https://himalayas.app/jobs/api?limit=50&q={urllib.parse.quote(q)}")
        except Exception:  # noqa: BLE001
            continue
        for j in d.get("jobs", []):
            out.append({
                "title": j.get("title", ""), "company": j.get("companyName", ""),
                "url": j.get("applicationLink")
                       or f"https://himalayas.app/companies/{j.get('companySlug','')}",
                "source": "himalayas", "posted": str(j.get("pubDate", "")),
                "text": clean(j.get("excerpt") or j.get("description", ""))[:1400],
                "extra": " ".join([*(j.get("categories") or []),
                                   *(j.get("locationRestrictions") or []),
                                   str(j.get("seniority", ""))]),
            })
    return out


def src_arbeitnow(cfg) -> list[dict]:
    out = []
    try:
        d = jget("https://www.arbeitnow.com/api/job-board-api")
    except Exception:  # noqa: BLE001
        return out
    for j in d.get("data", []):
        if not j.get("remote"):
            continue  # on-site only is useless when working remotely
        created = j.get("created_at")
        out.append({
            "title": j.get("title", ""), "company": j.get("company_name", ""),
            "url": j.get("url", ""), "source": "arbeitnow",
            "posted": datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                      if created else "",
            "text": clean(j.get("description", ""))[:1400],
            "extra": " ".join(j.get("tags") or []) + " remote "
                     + " ".join(j.get("location") or []),
        })
    return out


def src_wwr(cfg) -> list[dict]:
    out = []
    for slug in ("remote-programming-jobs", "remote-devops-sysadmin-jobs",
                 "remote-full-stack-programming-jobs"):
        try:
            out += _rss(f"https://weworkremotely.com/categories/{slug}.rss", "weworkremotely")
        except Exception:  # noqa: BLE001
            continue
    return out


def src_hn_hiring(cfg) -> list[dict]:
    """HN 'Who is hiring' via the free Algolia API — the highest-signal free
    source: founders post directly, no recruiter, no marketplace fee."""
    try:
        s = jget("https://hn.algolia.com/api/v1/search_by_date?"
                 "query=Ask%20HN%3A%20Who%20is%20hiring&tags=story&hitsPerPage=40")
    except Exception:  # noqa: BLE001
        return []
    stories = [h for h in s.get("hits", [])
               if re.match(r"^Ask HN: Who is hiring", h.get("title", ""))]
    if not stories:
        return []
    stories.sort(key=lambda h: h.get("created_at_i", 0), reverse=True)
    story = stories[0]
    if (time.time() - story.get("created_at_i", 0)) / 86400 > 45:
        print("    · hn-hiring: newest thread is stale — skipping")
        return []

    out = []
    for page in (0, 1, 2):
        try:
            c = jget("https://hn.algolia.com/api/v1/search?"
                     f"tags=comment,story_{story['objectID']}&hitsPerPage=100&page={page}")
        except Exception:  # noqa: BLE001
            break
        for h in c.get("hits", []):
            body = clean(h.get("comment_text") or "")
            if len(body) < 60:
                continue
            parts = [p.strip() for p in body.split("|")]
            company = parts[0][:60] if parts and 2 < len(parts[0]) < 60 else body[:40]
            role = (parts[1] if len(parts) > 1 and 3 < len(parts[1]) < 90
                    else "Hiring — see thread")
            out.append({
                "title": role, "company": company,
                "url": f"https://news.ycombinator.com/item?id={h.get('objectID','')}",
                "source": "hn-hiring", "posted": h.get("created_at", ""),
                "text": body[:1400], "extra": f"story {story['objectID']}",
            })
        if page >= int(c.get("nbPages", 1)) - 1:
            break
    return out


def src_hn_jobs(cfg) -> list[dict]:
    try:
        return _rss("https://hnrss.org/jobs", "hn-jobs")
    except Exception:  # noqa: BLE001
        return []


def src_reddit(cfg) -> list[dict]:
    """Reddit — .rss ONLY, never .json (403 from datacenter IPs).
    One sub at a time with an 8s gap; a 429 is tolerated as "no data"."""
    out = []
    for sub in cfg.get("reddit_subs", [])[:2]:
        try:
            out += _rss(f"https://www.reddit.com/r/{sub}/new/.rss", f"reddit/r/{sub}")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(8)
    return out


def src_github_issues(cfg) -> list[dict]:
    out = []
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    for q in ('"hiring" "remote" in:body type:issue state:open',
              'label:"help wanted" "freelance" in:body type:issue state:open'):
        url = ("https://api.github.com/search/issues?q=" + urllib.parse.quote(q)
               + "&sort=created&order=desc&per_page=30")
        headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
        for it in d.get("items", []):
            out.append({
                "title": it.get("title", ""),
                "company": (it.get("repository_url") or "").rstrip("/").split("/")[-1],
                "url": it.get("html_url", ""), "source": "github-issues",
                "posted": it.get("created_at", ""),
                "text": clean(it.get("body") or "")[:1400],
                "extra": " ".join(l["name"] for l in it.get("labels", [])
                                  if isinstance(l, dict)),
            })
    return out


def src_security_rss(cfg) -> list[dict]:
    out = []
    for url, name in [
        ("https://himalayas.app/jobs/rss", "himalayas-rss"),
        ("https://jobicy.com/?feed=job_feed&job_categories=dev", "jobicy-rss"),
        ("https://remotive.com/remote-jobs/feed/cybersecurity", "remotive-sec"),
    ]:
        try:
            out += _rss(url, name)
        except Exception:  # noqa: BLE001
            continue
    return out


SOURCES = {
    "remoteok": src_remoteok, "remotive": src_remotive, "jobicy": src_jobicy,
    "himalayas": src_himalayas, "arbeitnow": src_arbeitnow, "weworkremotely": src_wwr,
    "hn-hiring": src_hn_hiring, "hn-jobs": src_hn_jobs, "reddit": src_reddit,
    "github-issues": src_github_issues, "security-rss": src_security_rss,
}


# ───────────────────────── filtering ─────────────────────────

SELLER_RE = re.compile(
    r"^\s*[\[\(]?\s*(for\s*hire|forhire|available\s*for\s*work|hire\s*me|"
    r"looking\s*for\s*work|freelancer\s*available|offering\s*(my\s*)?services)", re.I)

CONTENT_SERIES_RE = re.compile(
    r"\[(freelance\s*&\s*business|guide|article|newsletter|tutorial|"
    r"week\s*\d+|part\s*\d+|series)\]", re.I)


def reject_reason(lead: dict, cfg: dict) -> str | None:
    """Return why a lead is junk, or None if it is a real buyer-side lead.

    The seller-post filter is the single most important one: half of
    r/forhire is people advertising themselves, so '[For Hire] Full-Stack
    Developer' is a COMPETITOR. Four of them landed in the first digest.
    """
    title = lead.get("title", "")
    body = lead.get("text", "")
    hay = f"{title} {body}".lower()
    src = lead.get("source", "")

    if SELLER_RE.match(title):
        return "seller-post"
    if src.startswith("reddit/") and "[hiring]" not in hay and "hiring:" not in hay:
        return "no-hiring-tag"
    if not lead.get("url"):
        return "no-url"
    if len(f"{title} {body}".strip()) < 70:
        return "too-thin"
    for bad in cfg.get("block_terms", []):
        if bad in hay:
            return f"blocked:{bad}"

    # Role-title gate. Body text always name-drops the stack, so an
    # "Enterprise Account Executive" role scores high on the word "rag" in the
    # company blurb. Only builder/security titles are worth kk's time.
    tl = title.lower()
    if any(b in tl for b in cfg.get("block_title_terms", [])):
        return "non-technical-role"
    if not any(g in tl for g in cfg.get("must_match_title", [])):
        return "title-not-my-work"

    if CONTENT_SERIES_RE.search(title):
        return "content-series"

    max_age = cfg.get("max_age_days", 45)
    p = parse_date(str(lead.get("posted", "")))
    if p is not None:
        if p.tzinfo is None:
            p = p.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - p).days > max_age:
            return f"stale>{max_age}d"
    return None


# ───────────────────────── scoring ─────────────────────────

def classify_engagement(lead: dict) -> str:
    """Tag contract / fulltime / unknown.

    Honest finding: the big public APIs are overwhelmingly FULL-TIME permanent
    roles. We tag rather than pretend, and show contract first.
    'consultant'/'contractor' were removed — every description mentions them
    in passing, which mislabelled permanent roles as contract work.
    """
    hay = f"{lead.get('title','')} {lead.get('text','')} {lead.get('extra','')}".lower()
    contract = ("contract role", "contract position", "contract-to-hire",
                "contract to hire", "freelance", "part-time", "part time", "hourly",
                "project basis", "retainer", "short-term", "short term",
                "fixed-term", "fixed term", "b2b contract", "day rate",
                "3 month", "6 month", "12 month contract")
    fulltime = ("full-time", "full time", "permanent", "401k", "401(k)",
                "pto", "health insurance", "salaried", "stock options",
                "parental leave", "unlimited pto")
    c = sum(1 for t in contract if t in hay)
    f = sum(1 for t in fulltime if t in hay)
    if c > f:
        return "contract"
    if f:
        return "fulltime"
    if c:
        return "contract"
    return "unknown"


def parse_date(posted: str) -> datetime | None:
    """Every format the sources actually emit.

    RFC-822 ("Mon, 27 Oct 2026 10:00:00 +0000") is what RSS emits, and
    fromisoformat() cannot read it — that bug marked every RSS lead undated.
    """
    s = str(posted or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s[:10], fmt)
        except (ValueError, TypeError):
            continue
    return None


def score_lead(lead: dict, cfg: dict) -> tuple[int, list[str], str]:
    hay = " ".join([lead.get("title", ""), lead.get("company", ""),
                    lead.get("text", ""), lead.get("extra", "")]).lower()
    total, matched, cats = 0, [], {}

    for cat, spec in cfg["positive"].items():
        # Every category has an on/off switch, so categories can be tuned from
        # leads.config.json without touching code. Default True so an older
        # config without the flag keeps working unchanged.
        if not spec.get("enabled", True):
            continue
        hits = [t for t in spec["terms"] if t in hay]
        if hits:
            pts = min(len(hits), spec.get("cap", 3)) * spec["weight"]
            total += pts
            cats[cat] = pts
            matched += hits[:3]

    p = parse_date(str(lead.get("posted", "")))
    if p is not None:
        if p.tzinfo is None:
            p = p.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - p).total_seconds() / 3600
        if age_h <= 24:
            total += 6
            matched.append("<24h")
        elif age_h <= 72:
            total += 4
        elif age_h <= 168:
            total += 2
        elif age_h > 720:
            total -= 10

    for term in cfg["boost"]["terms"]:
        if term in hay:
            total += cfg["boost"]["weight"]
    for term in cfg["negative"]["terms"]:
        if term in hay:
            total += cfg["negative"]["weight"]

    primary = max(cats, key=lambda k: cats[k]) if cats else "general"

    eng = classify_engagement(lead)
    lead["engagement"] = eng
    if eng == "contract":
        total += 8
        matched.append("CONTRACT")
    elif eng == "fulltime":
        total -= 4
    return total, matched[:6], primary


# ─────────────────── outreach draft generation ───────────────────

DRAFTS = {
    "security": (
        "Hi {name} — saw the {title} post. I'm a security engineer and I do "
        "application/cloud security reviews. I'd start with a scoped threat model "
        "plus a dependency and auth-flow audit, then hand over a prioritised "
        "finding list rather than a PDF of CVEs. If it's still open I can send a "
        "2-3 day fixed-scope proposal. github.com/kkcyber2"),
    "ai": (
        "Hi {name} — your {title} post caught my eye. I build AI automation: RAG "
        "pipelines, agent workflows, and the unglamorous parts around them "
        "(retries, cost caps, evals). Recent work includes a self-verifying "
        "document Q&A system. Worth a 15-minute call? github.com/kkcyber2"),
    "web": (
        "Hi {name} — re: {title}. I build production full-stack apps "
        "(React/Node/Postgres, auth, tests, CI) and I've shipped a paid client "
        "site end to end — kelvarinstitches.com. I can scope this in phases with "
        "a fixed first milestone so there's no big up-front commitment. "
        "github.com/kkcyber2"),
    "devops": (
        "Hi {name} — saw {title}. I do DevOps and scripting work: CI pipelines, "
        "containerisation, and the ad-hoc automation nobody wants to maintain. I "
        "normally start by automating the single most painful manual step so you "
        "see value in week one. github.com/kkcyber2"),
    "aiml_eng": (
        "Hi {name} — re: {title}. I build LLM systems end to end, not just the "
        "prompt: retrieval, eval harnesses, cost caps, and the failure paths. "
        "I've shipped a self-verifying document Q&A system (two agents, revision "
        "loop). Happy to walk through the architecture. github.com/kkcyber2"),
    "threed": (
        "Hi {name} — saw {title}. I build interactive 3D on the web with Three.js "
        "/ React Three Fiber: product configurators, model viewers, and "
        "scroll-driven scenes that stay under a normal bundle budget. I care "
        "about frame rate on mid-range phones, which is where these fail. "
        "github.com/kkcyber2"),
    "gamedev": (
        "Hi {name} — your {title} post caught my eye. I do gameplay systems and "
        "tooling, and I'm comfortable in the unglamorous parts (save systems, "
        "netcode edge cases, build pipelines). Is this still open? "
        "github.com/kkcyber2"),
    "mobile": (
        "Hi {name} — re: {title}. I build cross-platform mobile with React Native "
        "/ Expo, including the release plumbing most people underestimate: store "
        "submission, OTA updates, crash triage. I can usually get a first build "
        "on a device in week one. github.com/kkcyber2"),
    "dataeng": (
        "Hi {name} — saw {title}. I build data pipelines that fail loudly instead "
        "of silently: idempotent loads, schema checks, backfills that don't need "
        "a babysitter. Recent work includes a DAG workflow engine with real retry "
        "and on-error policies. Worth a short call? github.com/kkcyber2"),
    "general": (
        "Hi {name} — I saw the {title} post. I work across security engineering, "
        "AI automation, and full-stack development, so I'm usually useful where "
        "those overlap. Is this still open? Background: github.com/kkcyber2 — "
        "recent paid client: kelvarinstitches.com"),
}


def make_draft(lead: dict, primary: str) -> str:
    name = re.split(r"[|,(\[]", lead.get("company") or "there")[0].strip()[:40] or "there"
    return DRAFTS.get(primary, DRAFTS["general"]).format(
        name=name, title=clip(lead.get("title", "role"), 60))


# ───────────────────────── delivery ─────────────────────────

def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def build_telegram(leads: list[dict], stats: dict) -> str:
    today = datetime.now(timezone.utc).strftime("%a %d %b")
    out = [f"<b>🎯 {len(leads)} leads · {today}</b>",
           f"<i>scanned {stats['scanned']} · kept {stats['kept']} · new {stats['new']}</i>", ""]
    for i, l in enumerate(leads, 1):
        out += [f"<b>{i}. [{l['score']}] {esc(clip(l['title'], 66))}</b>",
                f"{esc(clip(l['company'], 38))} · <i>{esc(l['source'])}</i> · {esc(l['age'])}",
                f'<a href="{esc(l["url"])}">open posting</a>', ""]
    out.append("<i>Drafts in out/digest.md — always edit before sending.</i>")
    return "\n".join(out)


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("  · telegram: skipped (no token/chat)")
        return False
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        body = urllib.parse.urlencode({
            "chat_id": chat, "text": chunk, "parse_mode": "HTML",
            "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status != 200:
                    print(f"  ! telegram: HTTP {r.status}")
                    return False
        except urllib.error.HTTPError as e:
            print(f"  ! telegram: {e.code} {e.read()[:180]!r}")
            return False
        time.sleep(1.2)
    print("  ✓ telegram sent")
    return True


def push_sheets(leads: list[dict], tab: str = "leads") -> bool:
    url = os.environ.get("SHEETS_WEBHOOK_URL", "").strip()
    if not url:
        print("  · sheets: skipped (no webhook)")
        return False
    cols = (("score", "title", "company", "source", "url", "age", "primary", "matched")
            if tab == "leads" else
            ("score", "business", "category", "city", "phone", "email", "website",
             "web_status", "problem", "social", "pitch"))
    payload = json.dumps({
        "token": os.environ.get("SHEETS_TOKEN", ""), "tab": tab,
        "rows": [{k: l.get(k, "") for k in cols} for l in leads]}).encode()
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, data=payload,
                                       headers={"Content-Type": "application/json"}),
                timeout=30) as r:
            print(f"  {'✓' if r.status == 200 else '!'} sheets: HTTP {r.status}")
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        print(f"  ! sheets: {e}")
        return False


# ───────────────────────── state ─────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"seen": {}}


def save_state(state: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    STATE_PATH.write_text(json.dumps(state, indent=0))


def humanise_age(posted: str) -> str:
    p = parse_date(str(posted or ""))
    if p is None:
        return "undated"
    if p.tzinfo is None:
        p = p.replace(tzinfo=timezone.utc)
    h = (datetime.now(timezone.utc) - p).total_seconds() / 3600
    if h < 1:
        return "just now"
    return f"{int(h)}h ago" if h < 24 else f"{int(h / 24)}d ago"


# ───────────────────────── main ─────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="leadbot — free lead generation")
    ap.add_argument("--dry-run", action="store_true",
                    help="print, send nothing, keep no state")
    ap.add_argument("--source", help="run one source only")
    ap.add_argument("--local", action="store_true",
                    help="run ONLY the local-business pipeline")
    ap.add_argument("--local-market", help="limit local pipeline to one city")
    ap.add_argument("--no-local", action="store_true",
                    help="run ONLY the job pipeline")
    args = ap.parse_args()

    # ── local business pipeline ──
    # `--source X` means "debug one job source", so the local pipeline is
    # skipped there: Overpass retries alone can take minutes, which made a
    # one-source smoke test look like a hang.
    local_leads: list[dict] = []
    if (args.local or not args.no_local) and not args.source:
        try:
            import localbot
            cfg_early = json.loads(CONFIG_PATH.read_text())
            if cfg_early.get("local", {}).get("enabled", False):
                print("\n--- LOCAL BUSINESS PIPELINE ---")
                local_leads = localbot.run(
                    cfg_early, dry_run=args.dry_run, only_market=args.local_market)
                print(f"--- local pipeline: {len(local_leads)} leads ---\n")
        except ImportError:
            print("  · local: localbot.py not found — skipping")
        except Exception as e:  # noqa: BLE001 — never let local kill the job run
            print(f"  ! local pipeline failed: {type(e).__name__}: {str(e)[:120]}")

    if args.local:
        if local_leads:
            send_telegram(localbot.build_telegram_local(local_leads))
            push_sheets(local_leads, tab="local")
        else:
            print("no local leads produced")
        return 0

    cfg = json.loads(CONFIG_PATH.read_text())
    state = load_state()
    OUT_DIR.mkdir(exist_ok=True)

    names = [args.source] if args.source else list(SOURCES)
    all_leads: list[dict] = []
    stats = {"scanned": 0, "kept": 0, "new": 0, "rejected": {}, "sources": {}}

    print(f"leadbot · {len(names)} sources · {'DRY RUN' if args.dry_run else 'live'}")
    for name in names:
        fn = SOURCES.get(name)
        if not fn:
            print(f"  ! unknown source {name}")
            continue
        t0 = time.time()
        try:
            got = fn(cfg)
        except Exception as e:  # noqa: BLE001 — one dead source must not kill the run
            print(f"  ✗ {name:<16} FAILED: {type(e).__name__}: {str(e)[:80]}")
            stats["sources"][name] = 0
            continue
        stats["sources"][name] = len(got)
        stats["scanned"] += len(got)
        all_leads += got
        print(f"  ✓ {name:<16} {len(got):>4} items  {time.time() - t0:>5.1f}s")

    scored = []
    for l in all_leads:
        why = reject_reason(l, cfg)
        if why:
            stats["rejected"][why] = stats["rejected"].get(why, 0) + 1
            continue
        s, matched, primary = score_lead(l, cfg)
        if s < cfg["min_score"]:
            stats["rejected"]["low-score"] = stats["rejected"].get("low-score", 0) + 1
            continue
        l.update(score=s, matched=", ".join(matched), primary=primary,
                 age=humanise_age(l.get("posted", "")),
                 key=norm_key(l["title"], l["company"]))
        scored.append(l)

    rank = {"contract": 0, "unknown": 1, "fulltime": 2}
    scored.sort(key=lambda x: (rank.get(x.get("engagement", "unknown"), 1), -x["score"]))
    stats["kept"] = len(scored)

    now = datetime.now(timezone.utc).isoformat()
    fresh, batch_keys, seen_titles = [], set(), set()
    for l in scored:
        if l["key"] in state["seen"] or l["key"] in batch_keys:
            continue
        tk = title_key(l.get("title", ""))
        if tk and tk in seen_titles:
            stats["rejected"]["duplicate-title"] = \
                stats["rejected"].get("duplicate-title", 0) + 1
            continue
        seen_titles.add(tk)
        batch_keys.add(l["key"])
        fresh.append(l)
    stats["new"] = len(fresh)

    top, per_source = [], {}
    cap = cfg.get("max_per_source", 3)
    limit = cfg.get("max_leads", 10)
    for l in fresh:
        if len(top) >= limit:
            break
        if per_source.get(l["source"], 0) >= cap:
            continue
        per_source[l["source"]] = per_source.get(l["source"], 0) + 1
        top.append(l)
    if len(top) < limit:
        have = {id(x) for x in top}
        for l in fresh:
            if len(top) >= limit:
                break
            if id(l) not in have:
                top.append(l)

    for l in top:
        l["draft"] = make_draft(l, l["primary"])

    (OUT_DIR / "latest.json").write_text(json.dumps(
        {"generated": now, "stats": stats, "leads": top}, indent=2))

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["score", "title", "company", "source", "age", "primary", "url", "matched"])
    for l in top:
        w.writerow([l["score"], l["title"], l["company"], l["source"],
                    l["age"], l["primary"], l["url"], l["matched"]])
    (OUT_DIR / "leads.csv").write_text(buf.getvalue())

    md = [f"# Lead digest — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}", "",
          f"Scanned **{stats['scanned']}** · passed **{stats['kept']}** · "
          f"new **{stats['new']}**", ""]
    for i, l in enumerate(top, 1):
        md += [f"## {i}. [{l['score']}] {l['title']}", "",
               f"- **Company:** {l['company']}",
               f"- **Source:** {l['source']} · {l['age']}",
               f"- **Matched:** {l['matched'] or '—'}",
               f"- **Link:** {l['url']}", "",
               "**Draft first message** (edit before sending):", "",
               f"> {l['draft']}", "", "---", ""]
    (OUT_DIR / "digest.md").write_text("\n".join(md))

    print(f"\nscanned {stats['scanned']} → passed {stats['kept']} → "
          f"new {stats['new']} → digest {len(top)}")
    print("rejected:", ", ".join(f"{k}={v}" for k, v in
                                 sorted(stats["rejected"].items(), key=lambda x: -x[1])[:6]))
    for i, l in enumerate(top, 1):
        print(f"  {i}. [{l['score']:>3}] {l['source']:<15} {clip(l['title'], 50)}")

    if args.dry_run:
        print("\n(dry run — nothing sent, state unchanged)")
        return 0

    send_telegram(build_telegram(top, stats))
    push_sheets(top)
    if local_leads:
        send_telegram(localbot.build_telegram_local(local_leads))
        push_sheets(local_leads, tab="local")
    for l in top:
        state["seen"][l["key"]] = now
    save_state(state)
    print("state saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
