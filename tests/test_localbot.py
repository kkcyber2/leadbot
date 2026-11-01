"""Unit tests for localbot.py — the local-business pipeline.

Covers the pure logic: bbox maths, OSM normalisation, the chain filter, the
site-audit signal detection (with the network mocked), scoring, and pitch
generation. Nothing here touches the internet, so CI is deterministic.
"""
from __future__ import annotations

import io
import ssl
import urllib.error
from datetime import datetime


# ─────────────────────────────────────────────── bbox ──

def test_bbox_is_centred(localbot):
    s, w, n, e = localbot.bbox(25.0, 55.0, 10.0)
    assert abs((s + n) / 2 - 25.0) < 1e-9
    assert abs((w + e) / 2 - 55.0) < 1e-9


def test_bbox_span_matches_requested_size(localbot):
    """`size_km` is the half-width: dlat is subtracted AND added to the centre."""
    s, w, n, e = localbot.bbox(25.0, 55.0, 10.0)
    assert abs((n - s) - (2 * 10.0 / 111.0)) < 1e-6


def test_bbox_longitude_widens_toward_poles(localbot):
    """At high latitude a degree of longitude is shorter, so the box must be wider."""
    _, w_low, _, e_low = localbot.bbox(0.0, 0.0, 10.0)
    _, w_high, _, e_high = localbot.bbox(60.0, 0.0, 10.0)
    assert (e_high - w_high) > (e_low - w_low)


def test_bbox_survives_the_pole(localbot):
    """cos(90) is 0 — the guard must stop a divide-by-zero."""
    s, w, n, e = localbot.bbox(89.99, 0.0, 10.0)
    assert all(isinstance(x, float) for x in (s, w, n, e))
    assert e > w


# ─────────────────────────────────────────────── normalise ──

TRADE_MAP = {
    "dentist": {"tag": "amenity", "value": "dentist", "label": "Dentist", "budget": "high"},
    "cafe": {"tag": "amenity", "value": "cafe", "label": "Cafe", "budget": "mid"},
}
MARKET = {"name": "Dubai", "country": "AE", "currency": "AED", "priority": 1}


def _el(**tags):
    return {"type": "node", "id": 12345, "tags": tags}


def test_normalise_maps_a_dentist(localbot):
    b = localbot.normalise(_el(name="Smile Clinic", amenity="dentist"), TRADE_MAP, MARKET)
    assert b["business"] == "Smile Clinic"
    assert b["category"] == "Dentist"
    assert b["budget_band"] == "high"
    assert b["city"] == "Dubai"
    assert b["currency"] == "AED"


def test_normalise_rejects_unnamed(localbot):
    assert localbot.normalise(_el(amenity="cafe"), TRADE_MAP, MARKET) is None


def test_normalise_rejects_one_char_name(localbot):
    assert localbot.normalise(_el(name="X", amenity="cafe"), TRADE_MAP, MARKET) is None


def test_normalise_falls_back_to_english_name(localbot):
    """A local-script `name` wins; `name:en` is only the fallback."""
    # name:en alone, no local name
    b = localbot.normalise(_el(**{"name:en": "Blue Cafe"}, amenity="cafe"),
                           TRADE_MAP, MARKET)
    assert b["business"] == "Blue Cafe"
    # local name present -> it wins, as OSM records the on-the-ground name
    native = localbot.normalise(_el(name="\u0645\u0642\u0647\u0649",
                                     **{"name:en": "Blue Cafe"}, amenity="cafe"),
                                 TRADE_MAP, MARKET)
    assert native["business"] == "\u0645\u0642\u0647\u0649"


def test_normalise_collects_contact_fields(localbot):
    b = localbot.normalise(_el(name="Blue Cafe", amenity="cafe",
                               phone="+971500000000", email="a@b.co",
                               website="bluecafe.ae"), TRADE_MAP, MARKET)
    assert b["phone"] == "+971500000000"
    assert b["email"] == "a@b.co"
    assert b["website"] == "bluecafe.ae"


def test_normalise_reads_contact_prefixed_tags(localbot):
    b = localbot.normalise(_el(name="Blue Cafe", amenity="cafe",
                               **{"contact:phone": "+971511111111",
                                  "contact:email": "x@y.co"}), TRADE_MAP, MARKET)
    assert b["phone"] == "+971511111111"
    assert b["email"] == "x@y.co"


def test_normalise_builds_social_list(localbot):
    b = localbot.normalise(_el(name="Blue Cafe", amenity="cafe",
                               **{"contact:instagram": "@blue"}), TRADE_MAP, MARKET)
    assert "instagram:@blue" in b["social"]


def test_normalise_falls_back_to_generic_category(localbot):
    b = localbot.normalise(_el(name="Odd Shop", shop="hardware"), TRADE_MAP, MARKET)
    assert b["category"] == "hardware"


def test_normalise_keeps_osm_attribution(localbot):
    b = localbot.normalise(_el(name="Blue Cafe", amenity="cafe"), TRADE_MAP, MARKET)
    assert "OpenStreetMap" in b["osm_attribution"]


def test_normalise_builds_address(localbot):
    b = localbot.normalise(_el(name="Blue Cafe", amenity="cafe",
                               **{"addr:housenumber": "12", "addr:street": "Main St"}),
                           TRADE_MAP, MARKET)
    assert b["address"] == "12 Main St"


# ─────────────────────────────────────────────── chain filter ──

def test_is_chain_catches_known_brands(localbot, lcfg):
    for name in ("Pizza Hut", "Nando's", "KFC", "Starbucks", "McDonald's"):
        assert localbot.is_chain(name, lcfg), name


def test_is_chain_allows_independents(localbot, lcfg):
    for name in ("La Brioche", "Spaghetti House", "Bread Ahead", "Alshablan Medical Center"):
        assert not localbot.is_chain(name, lcfg), name


def test_is_chain_is_case_insensitive(localbot, lcfg):
    assert localbot.is_chain("PIZZA HUT", lcfg)
    assert localbot.is_chain("pizza hut", lcfg)


def test_is_chain_honours_config_blocklist(localbot, lcfg_copy):
    lcfg_copy["block_brands"] = ["my rival bakery"]
    assert localbot.is_chain("My Rival Bakery", lcfg_copy)


def test_is_chain_does_not_match_substrings_of_unrelated_words(localbot, lcfg):
    """'fab ' is in the list with a trailing space for exactly this reason."""
    assert not localbot.is_chain("Fabrication Studio", lcfg)


# ─────────────────────────────────────────────── biz_key ──

def test_biz_key_is_stable(localbot):
    a = {"business": "Blue Cafe", "city": "Dubai"}
    assert localbot.biz_key(a) == localbot.biz_key(dict(a))


def test_biz_key_ignores_punctuation(localbot):
    a = {"business": "Blue Cafe", "city": "Dubai"}
    b = {"business": "blue  cafe!", "city": "dubai"}
    assert localbot.biz_key(a) == localbot.biz_key(b)


def test_biz_key_differs_by_city(localbot):
    a = {"business": "Blue Cafe", "city": "Dubai"}
    b = {"business": "Blue Cafe", "city": "London"}
    assert localbot.biz_key(a) != localbot.biz_key(b)


# ─────────────────────────────────────────────── audit (mocked) ──

class _FakeResp(io.BytesIO):
    def __init__(self, body: bytes, status=200, url="https://x.test/"):
        super().__init__(body)
        self.status = status
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, localbot, *, body=b"", status=200, url="https://x.test/",
           exc=None):
    def fake_urlopen(req, timeout=None, context=None):
        if exc is not None:
            raise exc
        return _FakeResp(body, status, url)
    monkeypatch.setattr(localbot.urllib.request, "urlopen", fake_urlopen)


GOOD_HTML = b"""<html><head><title>Blue Cafe</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
</head><body><h1>Blue Cafe</h1>""" + b"<p>content</p>" * 2000 + b"""
<footer>&copy; 2026</footer></body></html>"""


def test_audit_no_url_is_status_none(localbot, lcfg):
    a = localbot.audit_site("", lcfg)
    assert a["web_status"] == "none"
    assert "no_website" in a["signals"]


def test_audit_good_site_is_ok(localbot, lcfg, monkeypatch):
    _patch(monkeypatch, localbot, body=GOOD_HTML)
    a = localbot.audit_site("https://x.test/", lcfg)
    assert a["web_status"] == "ok"
    assert a["problem"] == ""


def test_audit_detects_missing_viewport(localbot, lcfg, monkeypatch):
    html = GOOD_HTML.replace(b'<meta name="viewport" content="width=device-width,initial-scale=1">', b"")
    _patch(monkeypatch, localbot, body=html)
    a = localbot.audit_site("https://x.test/", lcfg)
    assert "no_viewport" in a["signals"]
    assert "mobile" in a["problem"].lower()


def test_audit_detects_http_only(localbot, lcfg, monkeypatch):
    _patch(monkeypatch, localbot, body=GOOD_HTML, url="http://x.test/")
    a = localbot.audit_site("http://x.test/", lcfg)
    assert "no_https" in a["signals"]
    assert "HTTPS" in a["problem"]


def test_audit_detects_stale_copyright(localbot, lcfg, monkeypatch):
    html = GOOD_HTML.replace(b"&copy; 2026", b"&copy; 2019")
    _patch(monkeypatch, localbot, body=html)
    a = localbot.audit_site("https://x.test/", lcfg)
    assert a["copy_year"] == 2019
    assert "stale_year" in a["signals"]


def test_audit_detects_wix(localbot, lcfg, monkeypatch):
    html = GOOD_HTML.replace(b"<h1>", b'<script src="https://static.wixstatic.com/x.js"></script><h1>')
    _patch(monkeypatch, localbot, body=html)
    a = localbot.audit_site("https://x.test/", lcfg)
    assert a["builder"] == "wix"
    assert "builder_site" in a["signals"]


def test_audit_detects_wordpress(localbot, lcfg, monkeypatch):
    html = GOOD_HTML.replace(b"<h1>", b'<link href="/wp-content/a.css"><h1>')
    _patch(monkeypatch, localbot, body=html)
    a = localbot.audit_site("https://x.test/", lcfg)
    assert a["builder"] == "wordpress"


def test_audit_http_error_is_broken(localbot, lcfg, monkeypatch):
    _patch(monkeypatch, localbot, exc=urllib.error.HTTPError("u", 404, "nf", {}, None))
    a = localbot.audit_site("https://x.test/", lcfg)
    assert a["web_status"] == "broken"
    assert "404" in a["problem"]


def test_audit_dns_failure_is_broken(localbot, lcfg, monkeypatch):
    _patch(monkeypatch, localbot, exc=urllib.error.URLError("dns"))
    a = localbot.audit_site("https://x.test/", lcfg)
    assert a["web_status"] == "broken"
    assert "unreachable" in a["problem"].lower()


def test_audit_tls_failure_is_broken(localbot, lcfg, monkeypatch):
    _patch(monkeypatch, localbot, exc=ssl.SSLError("bad cert"))
    a = localbot.audit_site("https://x.test/", lcfg)
    assert a["web_status"] == "broken"
    assert "certificate" in a["problem"].lower()


def test_audit_adds_scheme_when_missing(localbot, lcfg, monkeypatch):
    seen = {}

    def fake(req, timeout=None, context=None):
        seen["url"] = req.full_url
        return _FakeResp(GOOD_HTML)
    monkeypatch.setattr(localbot.urllib.request, "urlopen", fake)
    localbot.audit_site("bluecafe.ae", lcfg)
    assert seen["url"].startswith("http")


def test_audit_records_title(localbot, lcfg, monkeypatch):
    _patch(monkeypatch, localbot, body=GOOD_HTML)
    a = localbot.audit_site("https://x.test/", lcfg)
    assert a["title"] == "Blue Cafe"


# ─────────────────────────────────────────────── describe_problem ──

def test_describe_prefers_https_over_everything(localbot):
    msg = localbot.describe_problem(["no_https", "no_viewport", "stale_year"], {"copy_year": 2019})
    assert "HTTPS" in msg


def test_describe_mobile_second(localbot):
    msg = localbot.describe_problem(["no_viewport", "stale_year"], {"copy_year": 2019})
    assert "mobile" in msg.lower()


def test_describe_stale_includes_year_and_age(localbot):
    year = datetime.now().year - 4
    msg = localbot.describe_problem(["stale_year"], {"copy_year": year})
    assert str(year) in msg
    assert "4 years" in msg


def test_describe_builder_names_the_builder(localbot):
    msg = localbot.describe_problem(["builder_site"], {"builder": "wix"})
    assert "wix" in msg


def test_describe_slow_includes_ms(localbot):
    msg = localbot.describe_problem(["slow"], {"load_ms": 8000})
    assert "8000" in msg


def test_describe_falls_back(localbot):
    assert localbot.describe_problem(["unknown_signal"], {}) == "Existing site has multiple issues"


# ─────────────────────────────────────────────── scoring ──

def _biz(**kw):
    base = {"business": "Blue Cafe", "city": "Dubai", "web_status": "none",
            "signals": [], "phone": "", "email": "", "budget_band": "mid",
            "market_priority": 1}
    base.update(kw)
    return base


def test_score_no_website_is_high(localbot, lcfg):
    total, why = localbot.score(_biz(), lcfg)
    assert total >= 8
    assert "no site" in why


def test_score_broken_site(localbot, lcfg):
    total, why = localbot.score(_biz(web_status="broken"), lcfg)
    assert total >= 6
    assert "site down" in why


def test_score_ok_site_is_zero(localbot, lcfg):
    """A fine site means nobody to sell to — must not appear in the digest."""
    total, why = localbot.score(_biz(web_status="ok"), lcfg)
    assert total == 0
    assert why == ["already fine"]


def test_score_penalises_uncontactable(localbot, lcfg):
    with_phone = localbot.score(_biz(phone="+9715"), lcfg)[0]
    without = localbot.score(_biz(), lcfg)[0]
    assert with_phone > without


def test_score_penalty_applies_per_missing_contact_channel(localbot, lcfg):
    """The -5 fires on a missing phone, so phone+email beats email alone."""
    both = localbot.score(_biz(phone="+9715", email="a@b.co"), lcfg)[0]
    email_only = localbot.score(_biz(email="a@b.co"), lcfg)[0]
    phone_only = localbot.score(_biz(phone="+9715"), lcfg)[0]
    assert both > email_only
    assert both > phone_only


def test_score_rewards_premium_trade(localbot, lcfg):
    high = localbot.score(_biz(budget_band="high"), lcfg)[0]
    mid = localbot.score(_biz(budget_band="mid"), lcfg)[0]
    assert high > mid


def test_score_rewards_tier1_market(localbot, lcfg):
    t1 = localbot.score(_biz(market_priority=1), lcfg)[0]
    t3 = localbot.score(_biz(market_priority=9), lcfg)[0]
    assert t1 > t3


def test_score_accumulates_multiple_signals(localbot, lcfg):
    one = localbot.score(_biz(web_status="outdated", signals=["no_https"]), lcfg)[0]
    two = localbot.score(_biz(web_status="outdated", signals=["no_https", "no_viewport"]), lcfg)[0]
    assert two > one


def test_score_weights_are_configurable(localbot, lcfg_copy):
    base = localbot.score(_biz(), lcfg_copy)[0]
    lcfg_copy["score_weights"]["no_website"] = 50
    assert localbot.score(_biz(), lcfg_copy)[0] == base + 36


# ─────────────────────────────────────────────── pitch ──

def test_pitch_none_mentions_no_site(localbot):
    out = localbot.make_pitch(_biz(web_status="none"))
    assert "Blue Cafe" in out
    assert len(out) > 80


def test_pitch_broken_mentions_the_outage(localbot):
    out = localbot.make_pitch(_biz(web_status="broken", problem="Site returns HTTP 404"))
    assert "Blue Cafe" in out


def test_pitch_no_viewport_is_specific(localbot):
    out = localbot.make_pitch(_biz(web_status="outdated", signals=["no_viewport"],
                                   problem="No mobile viewport"))
    assert "phone" in out.lower() or "mobile" in out.lower()


def test_pitch_stale_mentions_the_year(localbot):
    year = datetime.now().year - 3
    out = localbot.make_pitch(_biz(web_status="outdated", signals=["stale_year"],
                                   copy_year=year, problem=f"Footer still says {year}"))
    assert str(year) in out


def test_pitch_always_names_the_business(localbot):
    for status, sigs in (("none", []), ("broken", []),
                         ("outdated", ["no_https"]), ("outdated", ["no_viewport"]),
                         ("outdated", ["stale_year"]), ("outdated", ["builder_site"]),
                         ("outdated", ["slow"]), ("outdated", ["thin_content"])):
        out = localbot.make_pitch(_biz(web_status=status, signals=sigs, copy_year=2019))
        assert "Blue Cafe" in out, (status, sigs)


def test_every_pitch_key_is_reachable(localbot):
    """Each PITCH entry must be selectable, or it is dead code."""
    assert set(localbot.PITCH) >= {"none", "broken", "no_https", "no_viewport",
                                   "stale_year", "builder_site", "slow",
                                   "thin_content", "generic"}


# ─────────────────────────────────────────────── config sanity ──

def test_local_config_has_markets(cfg):
    assert len(cfg["local"]["markets"]) >= 5


def test_local_config_markets_have_required_fields(cfg):
    for m in cfg["local"]["markets"]:
        assert m.get("name"), m
        assert "priority" in m, m


def test_local_config_has_trades(cfg):
    assert len(cfg["local"]["trades"]) >= 10


def test_local_config_trades_have_tag_value_label(cfg):
    for t in cfg["local"]["trades"]:
        assert t.get("tag") and t.get("value") and t.get("label"), t


def test_local_config_score_weights_cover_all_signals(cfg):
    w = cfg["local"]["score_weights"]
    for key in ("no_website", "broken", "no_https", "no_viewport",
                "stale_year", "builder_site", "slow", "thin_content"):
        assert key in w, key


def test_local_config_audit_workers_is_sane(cfg):
    assert 1 <= cfg["local"]["audit_workers"] <= 20


def test_local_config_overpass_endpoints_are_https(cfg):
    for ep in cfg["local"]["overpass_endpoints"]:
        assert ep.startswith("https://"), ep
