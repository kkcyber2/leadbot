"""Unit tests for leadbot.py — the job-board pipeline.

These test the pure logic: text cleaning, dedupe keys, feed parsing, the
rejection gate, engagement classification, date parsing, scoring, and draft
generation. No network calls: every source function is exercised through
fixtures or monkeypatched, so the suite runs offline in CI.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


# ─────────────────────────────────────────────── text helpers ──

def test_clean_strips_tags_and_collapses_whitespace(leadbot):
    assert leadbot.clean("<p>Hello   <b>world</b></p>") == "Hello world"


def test_clean_unescapes_entities(leadbot):
    assert leadbot.clean("R&amp;D &lt;team&gt;") == "R&D <team>"


def test_clean_handles_none_and_empty(leadbot):
    assert leadbot.clean(None) == ""
    assert leadbot.clean("") == ""


def test_clip_truncates_with_ellipsis(leadbot):
    out = leadbot.clip("a" * 100, 10)
    assert len(out) == 10
    assert out.endswith("\u2026")


def test_clip_leaves_short_strings_alone(leadbot):
    assert leadbot.clip("short", 50) == "short"


def test_clip_cleans_before_measuring(leadbot):
    # tags must not count toward the length budget
    assert leadbot.clip("<b>hi</b>", 10) == "hi"


# ─────────────────────────────────────────────── dedupe keys ──

def test_norm_key_is_stable(leadbot):
    a = leadbot.norm_key("Senior Security Engineer", "Acme Ltd")
    b = leadbot.norm_key("Senior Security Engineer", "Acme Ltd")
    assert a == b


def test_norm_key_ignores_punctuation_and_case(leadbot):
    a = leadbot.norm_key("Senior Security Engineer", "Acme Ltd")
    b = leadbot.norm_key("senior security engineer!", "ACME, LTD.")
    assert a == b


def test_norm_key_differs_by_company(leadbot):
    a = leadbot.norm_key("Security Engineer", "Acme")
    b = leadbot.norm_key("Security Engineer", "Globex")
    assert a != b


def test_norm_key_is_16_hex_chars(leadbot):
    k = leadbot.norm_key("x", "y")
    assert len(k) == 16
    assert all(c in "0123456789abcdef" for c in k)


# ─────────────────────────────────────────────── feed parsing ──

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
  <title>Acme Corp: Senior Backend Engineer</title>
  <link>https://example.com/jobs/1</link>
  <description><![CDATA[<p>Python, Postgres, contract role.</p>]]></description>
  <pubDate>Mon, 27 Oct 2026 10:00:00 +0000</pubDate>
</item>
<item>
  <title>No Link Item</title>
  <description>should be skipped</description>
</item>
</channel></rss>"""


def test_parse_feed_extracts_items(leadbot):
    items = leadbot.parse_feed(RSS_SAMPLE)
    assert len(items) == 1, "item without a link must be dropped"
    it = items[0]
    assert it["url"] == "https://example.com/jobs/1"
    assert it["company"] == "Acme Corp"
    assert it["title"] == "Senior Backend Engineer"


def test_parse_feed_reads_cdata_body(leadbot):
    it = leadbot.parse_feed(RSS_SAMPLE)[0]
    assert "Python" in it["text"]
    assert "<p>" not in it["text"], "CDATA body must be cleaned of tags"


def test_parse_feed_handles_atom_entries(leadbot):
    atom = """<feed><entry>
      <title>Security Engineer</title>
      <link href="https://example.com/a"/>
      <summary>AppSec work</summary>
      <updated>2026-10-27T10:00:00Z</updated>
    </entry></feed>"""
    items = leadbot.parse_feed(atom)
    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/a"


def test_parse_feed_returns_empty_on_garbage(leadbot):
    assert leadbot.parse_feed("not xml at all") == []


# ─────────────────────────────────────────────── rejection gate ──

def _lead(**kw):
    base = {
        "title": "Senior Security Engineer",
        "company": "Acme",
        "url": "https://example.com/j",
        "text": "We need an application security engineer for a contract role. " * 3,
        "posted": datetime.now(timezone.utc).isoformat(),
        "source": "remoteok",
        "extra": "",
    }
    base.update(kw)
    return base


def test_reject_accepts_a_real_lead(leadbot, cfg):
    assert leadbot.reject_reason(_lead(), cfg) is None


def test_reject_drops_seller_posts(leadbot, cfg):
    """The bug that put four competitors in the first digest."""
    for title in ("[For Hire] Full-Stack Developer",
                  "For Hire: React dev",
                  "Available for work - security",
                  "Hire me for automation"):
        assert leadbot.reject_reason(_lead(title=title), cfg) == "seller-post", title


def test_reject_requires_hiring_tag_on_reddit(leadbot, cfg):
    lead = _lead(source="reddit/forhire", title="Security Engineer needed",
                 text="Looking for someone to do a pentest, details inside here.")
    assert leadbot.reject_reason(lead, cfg) == "no-hiring-tag"


def test_reject_allows_reddit_with_hiring_tag(leadbot, cfg):
    lead = _lead(source="reddit/forhire", title="[Hiring] Security Engineer",
                 text="We are hiring a security engineer for a contract role, remote ok.")
    assert leadbot.reject_reason(lead, cfg) is None


def test_reject_drops_missing_url(leadbot, cfg):
    assert leadbot.reject_reason(_lead(url=""), cfg) == "no-url"


def test_reject_drops_thin_content(leadbot, cfg):
    assert leadbot.reject_reason(_lead(text="hi"), cfg) == "too-thin"


def test_reject_drops_non_technical_titles(leadbot, cfg):
    """Body text name-drops the stack, so the title gate is what saves us."""
    lead = _lead(title="Enterprise Account Executive",
                 text="Sell our RAG and LLM platform to enterprises. Python, AI, security. " * 3)
    assert leadbot.reject_reason(lead, cfg) == "non-technical-role"


def test_reject_drops_titles_outside_my_work(leadbot, cfg):
    lead = _lead(title="Warehouse Operative",
                 text="Pick and pack orders in our fulfilment centre, shift work available. " * 3)
    assert leadbot.reject_reason(lead, cfg) == "title-not-my-work"


def test_reject_drops_content_series(leadbot, cfg):
    lead = _lead(title="[Freelance & Business] Winning Clients - Part 1",
                 text="A newsletter series about winning clients as a freelancer. " * 3)
    assert leadbot.reject_reason(lead, cfg) == "content-series"


def test_reject_drops_stale_leads(leadbot, cfg):
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    assert leadbot.reject_reason(_lead(posted=old), cfg) == "stale>45d"


def test_reject_keeps_recent_leads(leadbot, cfg):
    fresh = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert leadbot.reject_reason(_lead(posted=fresh), cfg) is None


def test_reject_honours_block_terms(leadbot, cfg_copy):
    cfg_copy["block_terms"] = ["unpaid"]
    lead = _lead(text="This is an unpaid internship style arrangement for a security engineer. " * 2)
    assert leadbot.reject_reason(lead, cfg_copy) == "blocked:unpaid"


# ─────────────────────────────────────────────── engagement ──

def test_classify_contract(leadbot):
    lead = _lead(text="This is a contract role, 6 month engagement, day rate negotiable.")
    assert leadbot.classify_engagement(lead) == "contract"


def test_classify_fulltime(leadbot):
    lead = _lead(text="Full-time permanent position with 401k, health insurance and PTO.")
    assert leadbot.classify_engagement(lead) == "fulltime"


def test_classify_unknown(leadbot):
    lead = _lead(text="We are looking for an engineer to join the platform team.")
    assert leadbot.classify_engagement(lead) == "unknown"


def test_classify_ties_resolve_by_count_not_by_order(leadbot):
    """classify_engagement returns contract only when c > f; a tie falls to
    fulltime because the `if f:` branch is checked first. Pinning that
    behaviour so a refactor cannot silently flip it."""
    tie = _lead(text="Contract role, day rate. Full-time permanent, 401k.")
    assert leadbot.classify_engagement(tie) == "fulltime"
    contract_heavy = _lead(text="Contract role, freelance, day rate, retainer, "
                                "project basis. Full-time 401k.")
    assert leadbot.classify_engagement(contract_heavy) == "contract"


# ─────────────────────────────────────────────── date parsing ──

def test_parse_date_rfc822(leadbot):
    """The bug that silently marked every RSS lead 'undated'."""
    d = leadbot.parse_date("Mon, 27 Oct 2026 10:00:00 +0000")
    assert d is not None
    assert d.year == 2026 and d.month == 10 and d.day == 27


def test_parse_date_iso_with_z(leadbot):
    d = leadbot.parse_date("2026-10-27T10:00:00Z")
    assert d is not None and d.tzinfo is not None


def test_parse_date_plain_date(leadbot):
    d = leadbot.parse_date("2026-10-27")
    assert d is not None and d.day == 27


def test_parse_date_returns_none_on_junk(leadbot):
    assert leadbot.parse_date("not a date") is None
    assert leadbot.parse_date("") is None
    assert leadbot.parse_date(None) is None


# ─────────────────────────────────────────────── scoring ──

def test_score_rewards_security_terms(leadbot, cfg):
    lead = _lead(title="Application Security Engineer",
                 text="AppSec, threat model, penetration testing, OWASP, secure code review. " * 2)
    total, matched, primary = leadbot.score_lead(lead, cfg)
    assert total > 0
    assert primary == "security"


def test_score_rewards_ai_terms(leadbot, cfg):
    lead = _lead(title="AI Automation Engineer",
                 text="Build RAG pipelines, LLM agents, prompt engineering, vector search. " * 2)
    total, matched, primary = leadbot.score_lead(lead, cfg)
    assert total > 0
    assert primary in ("ai", "aiml_eng")


def test_score_penalises_negative_terms(leadbot, cfg):
    good = _lead(title="Security Engineer",
                 text="AppSec, threat model, penetration testing, OWASP. " * 2)
    bad = _lead(title="Security Engineer",
                text="AppSec, threat model, penetration testing, OWASP. " * 2
                + " This is an unpaid volunteer role with no budget.")
    assert leadbot.score_lead(bad, cfg)[0] < leadbot.score_lead(good, cfg)[0]


def test_score_bonus_for_fresh_posting(leadbot, cfg):
    fresh = _lead(posted=datetime.now(timezone.utc).isoformat())
    old = _lead(posted=(datetime.now(timezone.utc) - timedelta(days=20)).isoformat())
    assert leadbot.score_lead(fresh, cfg)[0] > leadbot.score_lead(old, cfg)[0]


def test_score_contract_beats_fulltime(leadbot, cfg):
    contract = _lead(text="Contract role, day rate, 6 month engagement. Security, AppSec, OWASP. " * 2)
    fulltime = _lead(text="Full-time permanent, 401k, PTO, health insurance. Security, AppSec, OWASP. " * 2)
    assert leadbot.score_lead(contract, cfg)[0] > leadbot.score_lead(fulltime, cfg)[0]


def test_disabled_category_contributes_nothing(leadbot, cfg_copy):
    """The v2 on/off switch must actually stop scoring."""
    lead = _lead(title="Game Developer",
                 text="Unity, Unreal, gameplay systems, C# scripting for our studio. " * 2)
    cfg_copy["positive"]["gamedev"]["enabled"] = True
    on = leadbot.score_lead(lead, cfg_copy)[0]
    cfg_copy["positive"]["gamedev"]["enabled"] = False
    off = leadbot.score_lead(lead, cfg_copy)[0]
    assert on > off, "enabling gamedev must raise the score"


def test_score_sets_engagement_on_lead(leadbot, cfg):
    lead = _lead(text="Contract role, day rate. Security engineer needed. " * 2)
    leadbot.score_lead(lead, cfg)
    assert lead["engagement"] == "contract"


def test_score_returns_at_most_six_matched_terms(leadbot, cfg):
    lead = _lead(title="Security AI Full-Stack DevOps Engineer",
                 text="AppSec OWASP pentest RAG LLM React Node Docker Kubernetes CI/CD " * 3)
    _, matched, _ = leadbot.score_lead(lead, cfg)
    assert len(matched) <= 6


# ─────────────────────────────────────────────── drafts ──

def test_make_draft_uses_category_template(leadbot):
    lead = _lead(title="Security Engineer", company="Acme")
    out = leadbot.make_draft(lead, "security")
    assert "Acme" in out
    assert "security" in out.lower()


def test_make_draft_falls_back_to_general(leadbot):
    lead = _lead(title="Something Odd", company="Acme")
    out = leadbot.make_draft(lead, "no_such_category")
    assert "Acme" in out


def test_make_draft_handles_missing_company(leadbot):
    lead = _lead(company="")
    out = leadbot.make_draft(lead, "web")
    assert "there" in out


def test_every_category_has_a_draft(leadbot, cfg):
    """A category with no template would silently fall back to 'general'."""
    for cat in cfg["positive"]:
        assert cat in leadbot.DRAFTS, f"missing draft template for {cat}"


# ─────────────────────────────────────────────── config sanity ──

def test_config_has_nine_categories(cfg):
    assert len(cfg["positive"]) == 9


def test_config_categories_have_required_fields(cfg):
    for name, spec in cfg["positive"].items():
        assert "terms" in spec and spec["terms"], name
        assert "weight" in spec, name
        assert isinstance(spec.get("enabled", True), bool), name


def test_config_three_categories_off_by_default(cfg):
    off = [k for k, v in cfg["positive"].items() if not v.get("enabled", True)]
    assert sorted(off) == ["dataeng", "gamedev", "mobile"]


def test_config_title_gate_is_populated(cfg):
    assert len(cfg["must_match_title"]) > 20
    assert len(cfg["block_title_terms"]) > 20


def test_config_has_no_overlap_between_allow_and_block(cfg):
    """A term in both lists makes the gate order-dependent and unpredictable."""
    overlap = set(cfg["must_match_title"]) & set(cfg["block_title_terms"])
    assert not overlap, f"ambiguous title terms: {overlap}"


def test_config_sources_all_exist(leadbot):
    for name, fn in leadbot.SOURCES.items():
        assert callable(fn), name


def test_config_json_is_valid_and_roundtrips(cfg):
    assert json.loads(json.dumps(cfg)) == cfg
