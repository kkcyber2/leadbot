#!/usr/bin/env python3
"""
localbot — local-business lead discovery.
Finds businesses with NO website, a BROKEN website, or an OUTDATED one, audits
the site for objective technical signals, scores the lead, and drafts a pitch
that names the specific problem.

Standard library only — no pip install, no build step.

    python3 localbot.py --dry-run              # everything, print only
    python3 localbot.py --market Dubai         # one city
    python3 localbot.py --limit-markets 2      # cheapest possible run

WHY OVERPASS AND NOT GOOGLE PLACES
----------------------------------
Google Places bills per SKU since 1 March 2025: Nearby Search Pro $32/1,000,
free tiers per-SKU and non-pooling (10k Essentials / 5k Pro / 1k Enterprise).
Worse, the Maps ToS permits storing place IDs indefinitely and coordinates for
30 days — nothing else. A lead sheet of names and phone numbers is itself a
terms violation. Overpass/OpenStreetMap is ODbL: commercial use allowed, one
attribution line required, no storage limit.

WHAT THIS CANNOT DO (measured, not guessed)
-------------------------------------------
OSM has excellent coverage of existence and location, and poor coverage of
contact details. A live Dubai run: 36 named businesses, 6 with a website,
~8 with a phone. So ~83% come back "no site" — a genuine signal — but most
have nothing to contact them with. This file reports that in `contactable`
rather than pretending every row is actionable.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "leads.config.json"
STATE_PATH = ROOT / "state.local.json"
OUT_DIR = ROOT / "out"

# Overpass and Nominatim both 403 a default python-urllib UA. Nominatim's
# usage policy additionally requires a UA that identifies the application.
UA = "kk-localbot/2.0 (+local business research; github.com/kkcyber2)"
AUDIT_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36 kk-audit/2.0")
OSM_ATTRIBUTION = "© OpenStreetMap contributors"


# ───────────────────────── http ─────────────────────────

def _req(url, data=None, timeout=30, ua=UA):
    return urllib.request.Request(url, data=data, headers={
        "User-Agent": ua, "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })


def http_get(url, timeout=30, ua=UA, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(_req(url, timeout=timeout, ua=ua)) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2 + attempt * 3)
    raise last  # type: ignore[misc]


# ──────────────────── geo: Nominatim ────────────────────

_GEO_CACHE: dict[str, tuple[float, float]] = {}


def geocode(place: str, timeout=25) -> tuple[float, float] | None:
    """Place name -> (lat, lon). Nominatim allows 1 req/s and requires caching,
    so results are memoised in-process."""
    if place in _GEO_CACHE:
        return _GEO_CACHE[place]
    q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    try:
        d = json.loads(http_get(
            f"https://nominatim.openstreetmap.org/search?{q}", timeout=timeout))
        if d:
            hit = (float(d[0]["lat"]), float(d[0]["lon"]))
            _GEO_CACHE[place] = hit
            return hit
    except Exception as e:  # noqa: BLE001
        print(f"    ! geocode {place!r}: {type(e).__name__}")
    return None


def bbox(lat: float, lon: float, size_km: float) -> tuple[float, float, float, float]:
    """Square bbox `size_km` half-width around lat/lon (spans 2 × size_km)."""
    dlat = size_km / 111.0
    dlon = size_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


# ──────────────────── Overpass discovery ────────────────────

def overpass(query: str, cfg: dict) -> dict | None:
    """POST a query, rotating mirrors.

    Public Overpass 502s under load and times out on big bboxes — measured:
    3 failures before 1 success on a small cafe query. Rotate, back off, fail soft.
    """
    endpoints = cfg.get("overpass_endpoints") or ["https://overpass-api.de/api/interpreter"]
    retries = int(cfg.get("overpass_retries", 3))
    timeout = int(cfg.get("overpass_timeout_s", 90))
    for attempt in range(retries):
        ep = endpoints[attempt % len(endpoints)]
        try:
            body = urllib.parse.urlencode({"data": query}).encode()
            with urllib.request.urlopen(_req(ep, data=body, timeout=timeout)) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            print(f"    · overpass attempt {attempt + 1}/{retries} on "
                  f"{ep.split('/')[2]}: {type(e).__name__}")
            if attempt < retries - 1:
                time.sleep(3 + attempt * 4)
    return None


def build_query(trades: list[dict], box: tuple[float, float, float, float],
                limit: int) -> str:
    s, w, n, e = box
    parts = [f'  node["{t["tag"]}"="{t["value"]}"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});'
             for t in trades]
    return ("[out:json][timeout:80];\n(\n" + "\n".join(parts) +
            f"\n);\nout body {limit};")


def normalise(el: dict, trade_map: dict, market: dict) -> dict | None:
    """OSM element -> a business record. Returns None when unusable."""
    tags = el.get("tags") or {}
    name = (tags.get("name") or tags.get("name:en") or "").strip()
    if not name or len(name) < 2:
        return None

    kind, budget = "", "mid"
    for t in trade_map.values():
        if tags.get(t["tag"]) == t["value"]:
            kind, budget = t["label"], t["budget"]
            break
    if not kind:
        kind = (tags.get("shop") or tags.get("amenity")
                or tags.get("office") or "business").replace("_", " ")

    def pick(*keys):
        for k in keys:
            v = (tags.get(k) or "").strip()
            if v:
                return v
        return ""

    social = []
    for k, label in (("contact:instagram", "instagram"), ("contact:facebook", "facebook"),
                     ("contact:twitter", "twitter"), ("contact:linkedin", "linkedin")):
        v = pick(k)
        if v:
            social.append(f"{label}:{v}")

    addr = " ".join(x for x in (pick("addr:housenumber"), pick("addr:street"),
                                pick("addr:suburb")) if x)

    return {
        "business": name, "category": kind, "budget_band": budget,
        "city": market["name"], "country": market.get("country", ""),
        "currency": market.get("currency", "USD"),
        "market_priority": market.get("priority", 99),
        "phone": pick("phone", "contact:phone", "contact:mobile", "mobile"),
        "email": pick("email", "contact:email"),
        "website": pick("website", "contact:website", "url"),
        "address": addr, "social": " ".join(social),
        "osm_id": f'{el.get("type","node")}/{el.get("id","")}',
        "osm_attribution": OSM_ATTRIBUTION,
    }


# ──────────────────── site audit ────────────────────

BUILDERS = {
    "wix": (r"wix\.com", r"wixstatic", r"_wixCssImports", r"wix-code-sdk"),
    "squarespace": (r"squarespace\.com", r"static1\.squarespace", r"squarespace-cdn"),
    "godaddy": (r"godaddy\.com", r"websitebuilder", r"secureserver\.net"),
    "weebly": (r"weebly\.com", r"weeblysite"),
    "shopify": (r"cdn\.shopify\.com", r"shopify\.theme"),
    "wordpress": (r"wp-content", r"wp-includes"),
    "webflow": (r"webflow\.com", r"assets\.website-files\.com"),
}
COPY_YEAR = re.compile(r"(?:©|&copy;|copyright|\(c\))[^0-9]{0,30}(19|20)(\d{2})", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
VIEWPORT_RE = re.compile(r'<meta[^>]+name\s*=\s*["\']viewport', re.I)
H1_RE = re.compile(r"<h1[\s>]", re.I)


def audit_site(url: str, cfg: dict) -> dict:
    """Fetch a business site and return objective signals.

    Every signal is something a human can verify by opening the page. Nothing
    is inferred: if we cannot fetch it, we say broken and stop.
    """
    out = {"web_status": "", "problem": "", "signals": [], "https": False,
           "viewport": None, "builder": None, "copy_year": None,
           "load_ms": None, "kb": None, "title": "", "final_url": ""}

    if not url:
        out.update(web_status="none", problem="No website at all")
        out["signals"].append("no_website")
        return out

    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # an expired cert IS the finding

    timeout = int(cfg.get("audit_timeout_s", 10))
    t0 = time.time()
    try:
        with urllib.request.urlopen(
                _req(raw, timeout=timeout, ua=AUDIT_UA), context=ctx) as r:
            final = r.geturl()
            # 300KB covers <head> + a footer; avoids pulling video files
            body = r.read(300_000)
    except urllib.error.HTTPError as e:
        out.update(web_status="broken", problem=f"Site returns HTTP {e.code}")
        out["signals"].append("broken")
        return out
    except ssl.SSLError:
        out.update(web_status="broken",
                   problem="TLS/certificate failure — browsers show a warning")
        out["signals"].append("broken")
        return out
    except Exception as e:  # noqa: BLE001 — DNS failure, refused, timeout
        out.update(web_status="broken",
                   problem=f"Site unreachable ({type(e).__name__})")
        out["signals"].append("broken")
        return out

    out["load_ms"] = int((time.time() - t0) * 1000)
    out["final_url"] = final
    out["https"] = final.startswith("https://")
    html = body.decode("utf-8", "replace")
    out["kb"] = max(1, len(body) // 1024)

    m = TITLE_RE.search(html)
    out["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else ""
    out["viewport"] = bool(VIEWPORT_RE.search(html))

    for name, pats in BUILDERS.items():
        if any(re.search(p, html, re.I) for p in pats):
            out["builder"] = name
            break

    years = [int(a + b) for a, b in COPY_YEAR.findall(html)]
    if years:
        out["copy_year"] = max(years)

    sig = out["signals"]
    if not out["https"]:
        sig.append("no_https")
    if out["viewport"] is False:
        sig.append("no_viewport")
    if out["copy_year"] and out["copy_year"] < (
            datetime.now().year - int(cfg.get("stale_year_threshold", 2))):
        sig.append("stale_year")
    if out["builder"]:
        sig.append("builder_site")
    if (out["load_ms"] or 0) > int(cfg.get("slow_ms", 3500)):
        sig.append("slow")
    if len(body) < 20_000 and not H1_RE.search(html):
        sig.append("thin_content")

    if sig:
        out["web_status"] = "outdated"
        out["problem"] = describe_problem(sig, out)
    else:
        out["web_status"] = "ok"
        out["problem"] = ""
    return out


def describe_problem(sig: list[str], a: dict) -> str:
    """One human sentence naming the single most sellable defect."""
    if "no_https" in sig:
        return 'No HTTPS — browsers show "Not secure" in the address bar'
    if "no_viewport" in sig:
        return "No mobile viewport — renders as a shrunken desktop page on phones"
    if "stale_year" in sig and a.get("copy_year"):
        return (f"Footer still says {a['copy_year']} — "
                f"{datetime.now().year - a['copy_year']} years without a content update")
    if "builder_site" in sig:
        return f"Built on a page builder ({a['builder']}) — slow, template-limited"
    if "slow" in sig:
        return f"Slow first response ({a['load_ms']} ms)"
    if "thin_content" in sig:
        return f"Almost no content ({a['kb']} KB, no H1) — likely a placeholder page"
    return "Existing site has multiple issues"


# ──────────────────── scoring ────────────────────

def score(b: dict, cfg: dict) -> tuple[int, list[str]]:
    w = cfg.get("score_weights", {})
    status = b.get("web_status", "")
    sig: list[str] = list(b.get("signals") or [])
    total, why = 0, []

    if status == "none":
        total += w.get("no_website", 14)
        why.append("no site")
    elif status == "broken":
        total += w.get("broken", 12)
        why.append("site down")
    else:
        if "no_https" in sig:
            total += w.get("no_https", 9)
            why.append("no https")
        if "no_viewport" in sig:
            total += w.get("no_viewport", 8)
            why.append("not mobile")
        if "stale_year" in sig:
            total += w.get("stale_year", 7)
            why.append("stale")
        if "builder_site" in sig:
            total += w.get("builder_site", 5)
            why.append("builder")
        if "slow" in sig:
            total += w.get("slow", 4)
            why.append("slow")
        if "thin_content" in sig:
            total += w.get("thin_content", 4)
            why.append("thin")

    # A site with none of the above is a fine site — nobody to sell to.
    if status == "ok":
        return 0, ["already fine"]

    if b.get("phone"):
        total += w.get("has_phone", 4)
        why.append("phone")
    else:
        total += int(cfg.get("penalty_uncontactable", -5))
    if b.get("email"):
        total += w.get("has_email", 6)
        why.append("email")
    if b.get("budget_band") == "high":
        total += w.get("premium_trade", 3)
        why.append("high-budget trade")
    if int(b.get("market_priority", 99)) <= 4:
        total += w.get("tier1_market", 4)
        why.append("tier-1 market")
    return total, why


# ──────────────────── pitch generation ────────────────────

PITCH = {
    "none": (
        "Hi {name} — I was looking for {trade} in {city} and found your listing, "
        "but there's no website attached to it. I build small business sites for "
        "a living and I'm doing a few {city} ones at a reduced rate this month. "
        "One page, your menu/services, photos, click-to-call, and it shows up on "
        "Google. Would it help if I sent a one-page mock-up of what yours could "
        "look like? No charge for that."),
    "broken": (
        "Hi {name} — your website {domain} doesn't load at the moment "
        "({detail}). Not sure if you know. I fix sites like this — sometimes "
        "it's a lapsed domain or a host that's gone under, sometimes it's a "
        "plugin. If it's useful I can tell you exactly what's wrong and what it "
        "would cost to get it back up. I build {trade} sites in {city}."),
    "no_https": (
        "Hi {name} — quick heads-up on {domain}: it loads only over plain HTTP, "
        "so Chrome and Safari show \"Not secure\" next to your address. That "
        "costs you enquiries from people who don't trust it. It's usually a "
        "certificate and a redirect, not a rebuild — I can sort it in a day, and "
        "I do {trade} sites in {city}."),
    "no_viewport": (
        "Hi {name} — I opened {domain} on my phone and it came up as a shrunken "
        "desktop page you have to pinch to read. Most of your visitors are on a "
        "phone, so that's most of your enquiries. I redesign {trade} sites to be "
        "mobile-first — usually a focused rebuild of the page people actually "
        "land on rather than the whole site. Worth a look?"),
    "stale_year": (
        "Hi {name} — {domain} still says {year} in the footer. Nobody notices "
        "their own site ageing, but customers do — an old date reads as a closed "
        "shop. I do {trade} site refreshes in {city}: new photos, current hours "
        "and prices, mobile-clean. Small job, quick turnaround."),
    "builder_site": (
        "Hi {name} — {domain} runs on {builder}. It works, but it's slow and it "
        "limits what you can do with it. I rebuild {trade} sites off the page "
        "builders onto something fast you own outright, and I keep your existing "
        "look so regulars recognise you. Interested in a quote?"),
    "slow": (
        "Hi {name} — {domain} took {ms} ms to respond when I opened it, which is "
        "long enough that people leave before it paints. Usually a hosting or "
        "image-weight problem, not a design one. I do {trade} sites in {city} "
        "and I can tell you in ten minutes whether it's fixable cheaply."),
    "thin_content": (
        "Hi {name} — {domain} is basically a placeholder: almost no text, no "
        "headline, nothing Google can rank. For a {trade} in {city} that's a lot "
        "of lost walk-ins. I build straightforward sites that actually describe "
        "what you do. Can I send you a sketch?"),
    "generic": (
        "Hi {name} — I came across your {trade} in {city} and noticed {domain} "
        "could work a lot harder for you. I build sites for local businesses. "
        "Want me to send over a couple of specific things I'd change? Free, no "
        "pitch attached."),
}


def make_pitch(b: dict) -> str:
    key = "generic"
    if b.get("web_status") == "none":
        key = "none"
    elif b.get("web_status") == "broken":
        key = "broken"
    else:
        for sig_key in ("no_https", "no_viewport", "stale_year",
                        "builder_site", "slow", "thin_content"):
            if sig_key in (b.get("signals") or []):
                key = sig_key
                break

    domain = re.sub(r"^https?://", "", b.get("website") or "").split("/")[0] or "your site"
    return PITCH[key].format(
        name=b.get("business", "there"), trade=b.get("category", "business"),
        city=b.get("city", "the area"), domain=domain,
        detail=b.get("problem", "it isn't loading"),
        year=b.get("copy_year") or "an old year",
        builder=(b.get("builder") or "a page builder").title(),
        ms=b.get("load_ms") or 0)


# ──────────────────── state / dedupe ────────────────────

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {"seen": {}}


def save_state(state: dict, path: Path, keep_days: int = 180) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    path.write_text(json.dumps(state, indent=0))


def biz_key(b: dict) -> str:
    import hashlib
    basis = re.sub(r"[^a-z0-9]+", "",
                   (b.get("business", "") + b.get("city", "")).lower())
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


# ──────────────────── chain filter ────────────────────
# WHY THIS EXISTS (found in live testing, not theory)
# A missing `website` tag in OSM does NOT mean the business has no website.
# The Dubai test returned Pizza Hut, Nando's and Asado as "no website" leads —
# all obviously have sites; the contributor simply never filled the tag in.
# Chains also have no local decision-maker, so they are worthless leads.
CHAIN_NAMES = {
    "pizza hut", "domino's", "dominos", "kfc", "mcdonald's", "mcdonalds",
    "burger king", "subway", "starbucks", "costa coffee", "nando's", "nandos",
    "hardee's", "hardees", "popeyes", "taco bell", "papa john's", "papa johns",
    "dunkin", "baskin", "cold stone", "chili's", "chilis", "applebee's",
    "asado", "carrefour", "lulu hypermarket", "spinneys", "choithrams",
    "emirates nbd", "adcb", "fab ", "hsbc", "nissan", "toyota", "honda",
    "hyundai", "ford ", "chevrolet", "mitsubishi", "kia ", "renault",
    "shell", "enoc", "adnoc", "emarat", "boost", "tim hortons", "paul ",
    "the cheesecake factory", "p.f. chang", "shake shack", "five guys",
    "texas chicken", "al baik", "baskin-robbins", "seconds", "marrybrown",
}


def is_chain(name: str, cfg: dict) -> bool:
    low = " " + re.sub(r"\s+", " ", name.lower()).strip() + " "
    blocks = [c.lower() for c in (cfg.get("block_brands") or [])]
    for c in list(CHAIN_NAMES) + blocks:
        if c and c in low:
            return True
    return False


# ──────────────────── telegram digest ────────────────────

def build_telegram_local(leads: list[dict]) -> str:
    """Separate format from the job digest: a business has a problem to point
    at, not a job title to apply for."""
    import html as _html

    def esc(s):
        return _html.escape(str(s), quote=False)

    today = datetime.now(timezone.utc).strftime("%a %d %b")
    out = [f"<b>🏪 {len(leads)} local business leads · {today}</b>",
           "<i>businesses with no site, a broken site, or an outdated one</i>", ""]
    for i, l in enumerate(leads, 1):
        out.append(f"<b>{i}. [{l['score']}] {esc(_clip(l.get('business',''), 44))}</b>")
        out.append(f"{esc(l.get('category',''))} · {esc(l.get('city',''))}")
        out.append(f"⚠️ <i>{esc(l.get('web_status',''))}</i> — "
                   f"{esc(_clip(l.get('problem',''), 70))}")
        contact = []
        if l.get("phone"):
            contact.append(f"tel {esc(l['phone'])}")
        if l.get("email"):
            contact.append(f"mail {esc(l['email'])}")
        if l.get("website"):
            contact.append(f"web {esc(_clip(l['website'], 40))}")
        if contact:
            out.append(" · ".join(contact))
        out.append(f"💬 <i>{esc(_clip(l.get('pitch',''), 240))}</i>")
        out.append("")
    return "\n".join(out)


def _clip(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ──────────────────── main pipeline ────────────────────

def run(cfg: dict, *, dry_run: bool = False, only_market: str | None = None,
        state_path: Path | None = None, limit_markets: int | None = None) -> list[dict]:
    """Discover -> audit -> score -> rank. Returns the top leads."""
    lc = cfg.get("local", {})
    if not lc.get("enabled", False):
        return []

    state_path = state_path or STATE_PATH
    state = load_state(state_path)
    OUT_DIR.mkdir(exist_ok=True)

    trades = lc.get("trades", [])
    trade_map = {f'{t["tag"]}={t["value"]}': t for t in trades}
    markets = lc.get("markets", [])
    if only_market:
        markets = [m for m in markets if m["name"].lower() == only_market.lower()]
    markets = sorted(markets, key=lambda m: m.get("priority", 99))
    if limit_markets:
        markets = markets[:limit_markets]

    per_market = int(lc.get("max_businesses_per_market", 40))
    t_start = time.time()

    # ── 1. discovery ──
    found: list[dict] = []
    print(f"  discovery: {len(markets)} market(s), {len(trades)} trades each")
    for m in markets:
        box = bbox(m["lat"], m["lon"], float(m.get("size_km", 6)))
        d = overpass(build_query(trades, box, per_market), lc)
        if not d:
            print(f"    ✗ {m['name']}: overpass gave up")
            continue
        els = d.get("elements", [])
        rows = [r for r in (normalise(e, trade_map, m) for e in els) if r]
        print(f"    ✓ {m['name']:<12} {len(els):>3} elements → {len(rows):>3} named businesses")
        found += rows
        time.sleep(2)          # be a good OSM citizen

    if not found:
        print("  no businesses discovered")
        return []

    # ── 2. dedupe + drop chains ──
    fresh, seen_here, dupes, chains = [], set(), 0, 0
    for b in found:
        if is_chain(b.get("business", ""), lc):
            chains += 1
            continue
        k = biz_key(b)
        if k in state.get("seen", {}) or k in seen_here:
            dupes += 1
            continue
        seen_here.add(k)
        b["key"] = k
        fresh.append(b)
    print(f"  {len(found)} discovered · {dupes} already known · "
          f"{chains} chains dropped · {len(fresh)} new")

    # ── 3. audit the sites ──
    to_audit = [b for b in fresh if b.get("website")]
    no_site = [b for b in fresh if not b.get("website")]
    cap = int(lc.get("max_audits_per_run", 60))
    audit_targets = to_audit[:cap]
    skipped = len(to_audit) - len(audit_targets)
    print(f"  audit: {len(audit_targets)} sites "
          f"(+{len(no_site)} with no site, {skipped} deferred by cap)")

    done = 0
    if audit_targets:
        with ThreadPoolExecutor(max_workers=int(lc.get("audit_workers", 6))) as ex:
            futs = {ex.submit(audit_site, b["website"], lc): b for b in audit_targets}
            for f in as_completed(futs):
                b = futs[f]
                try:
                    b.update(f.result())
                except Exception as e:  # noqa: BLE001
                    b.update(web_status="broken",
                             problem=f"audit error ({type(e).__name__})",
                             signals=["broken"])
                done += 1
                if done % 10 == 0:
                    print(f"    … {done}/{len(audit_targets)} audited")
    for b in no_site:
        b.update(audit_site("", lc))

    # ── 4. score & rank ──
    scored, reject = [], {"ok": 0, "low": 0}
    for b in fresh:
        s, why = score(b, lc)
        if s <= 0:
            reject["ok"] += 1
            continue
        b["score"] = s
        b["triggers"] = ", ".join(why)
        if s < int(lc.get("min_score", 12)):
            reject["low"] += 1
            continue
        b["pitch"] = make_pitch(b)
        b["contactable"] = "yes" if (b.get("phone") or b.get("email")) else "no contact"
        scored.append(b)

    scored.sort(key=lambda x: (-x["score"], x.get("market_priority", 99)))
    top = scored[:int(lc.get("max_leads", 8))]

    elapsed = time.time() - t_start
    print(f"  scored {len(scored)} above floor "
          f"(rejected {reject['ok']} healthy sites, {reject['low']} low-score)")
    print(f"  → {len(top)} leads in {elapsed:.0f}s")
    for i, b in enumerate(top, 1):
        print(f"    {i}. [{b['score']:>3}] {b['business'][:32]:<32} "
              f"{b['web_status']:<8} {b['city']:<10} {b.get('contactable','')}")

    if not dry_run and top:
        (OUT_DIR / "local_latest.json").write_text(json.dumps({
            "generated": datetime.now(timezone.utc).isoformat(),
            "attribution": OSM_ATTRIBUTION,
            "stats": {"discovered": len(found), "new": len(fresh),
                      "audited": len(audit_targets), "leads": len(top),
                      "seconds": round(elapsed, 1)},
            "leads": top}, indent=2, ensure_ascii=False))
        now = datetime.now(timezone.utc).isoformat()
        for b in top:
            state.setdefault("seen", {})[b["key"]] = now
        save_state(state, state_path)

    return top


def main() -> int:
    ap = argparse.ArgumentParser(description="localbot — local business leads")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--market", help="one city, e.g. Dubai")
    ap.add_argument("--limit-markets", type=int, help="cap markets (cheap test run)")
    a = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    print(f"localbot v2 · {'DRY RUN' if a.dry_run else 'live'}")
    leads = run(cfg, dry_run=a.dry_run, only_market=a.market,
                limit_markets=a.limit_markets)
    if not leads:
        print("no leads — check the discovery step above")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
