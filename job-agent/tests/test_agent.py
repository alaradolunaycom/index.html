import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from job_agent import linkedin, llm, scoring, sources
from job_agent.models import Job, html_to_text
from job_agent.tracker import Tracker

PROFILE = yaml.safe_load((Path(__file__).resolve().parent.parent / "profile.yaml").read_text(encoding="utf-8"))
SEARCH = PROFILE["search"]
TODAY = date(2026, 9, 29)


def job(**kw):
    base = dict(source="test", company="Acme", title="Growth Marketing Manager", url="https://x/1",
                location="Remote - EMEA", description="B2B SaaS fintech, HubSpot, GTM, Series A",
                posted_at="2026-09-25", remote=True)
    base.update(kw)
    return Job(**base)


# --- parsing ---------------------------------------------------------------

def test_html_to_text_handles_greenhouse_double_escaping():
    assert html_to_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;B2B&lt;/li&gt;") == "Hello & welcome\nB2B"


def test_greenhouse_parser():
    data = {"jobs": [{"title": "Head of Growth", "absolute_url": "https://gh/1", "location": {"name": "Istanbul"},
                      "content": "&lt;p&gt;SaaS&lt;/p&gt;", "updated_at": "2026-09-20T10:00:00-04:00"}]}
    [j] = sources.greenhouse("acme", get=lambda url: data)
    assert (j.title, j.location, j.description, j.posted_at) == ("Head of Growth", "Istanbul", "SaaS", "2026-09-20")


def test_lever_parser_epoch_ms_and_remote():
    data = [{"text": "GTM Lead", "hostedUrl": "https://lv/1", "categories": {"location": "Europe"},
             "descriptionPlain": "B2B", "createdAt": 1758844800000, "workplaceType": "remote"}]
    [j] = sources.lever("acme", get=lambda url: data)
    assert j.remote is True and j.posted_at == "2025-09-26"


def test_ashby_parser():
    data = {"jobs": [{"title": "Product Marketing Manager", "jobUrl": "https://ab/1", "location": "London",
                      "isRemote": True, "descriptionPlain": "SaaS", "publishedAt": "2026-09-01T00:00:00Z",
                      "compensation": {"compensationTierSummary": "£60K – £75K"}}]}
    [j] = sources.ashby("acme", get=lambda url: data)
    assert j.salary == "£60K – £75K" and j.remote is True


def test_remoteok_skips_legal_notice():
    data = [{"legal": "notice"}, {"id": "1", "position": "Growth Lead", "company": "Co", "url": "https://ro/1",
                                  "location": "", "description": "<b>B2B</b>", "date": "2026-09-28T00:00:00+00:00",
                                  "salary_min": 70000, "salary_max": 90000}]
    [j] = sources.remoteok("marketing", get=lambda url: data)
    assert j.title == "Growth Lead" and j.salary == "$70,000–$90,000"


def test_weworkremotely_rss():
    rss = """<rss><channel><item><title>Acme: Demand Generation Manager</title>
    <link>https://wwr/1</link><region>Anywhere in the World</region>
    <description>&lt;p&gt;B2B SaaS&lt;/p&gt;</description>
    <pubDate>Mon, 28 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
    [j] = sources.weworkremotely("feed", get=lambda url: rss)
    assert (j.company, j.title, j.posted_at) == ("Acme", "Demand Generation Manager", "2026-09-28")


def test_collect_survives_failing_source(monkeypatch):
    monkeypatch.setattr(sources, "remotive", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sources, "remoteok", lambda t: [job(url=f"https://x/{t}")])
    cfg = {"aggregators": {"remotive_categories": ["marketing"], "remoteok_tags": ["a", "b"]}}
    jobs, errors = sources.collect(cfg)
    assert len(jobs) == 2 and errors == ["remotive:marketing: boom"]


# --- rule filter -------------------------------------------------------------

@pytest.mark.parametrize("title,ok", [
    ("Growth Marketing Manager", True),
    ("Head of Marketing", True),
    ("Senior Product Marketing Manager", True),
    ("Growth Engineer", False),          # excluded keyword wins
    ("VP Marketing", False),
    ("Account Executive", False),
    ("Office Manager", False),           # not in lane
    ("MVP Growth Lead", True),           # "vp" must not match inside "mvp"
])
def test_title_lane(title, ok):
    assert scoring.evaluate(job(title=title), SEARCH, TODAY).passed is ok


@pytest.mark.parametrize("location,desc,ok", [
    ("Remote - EMEA", "", True),
    ("Istanbul, Türkiye", "", True),
    ("Remote - US", "", False),
    ("Remote (US or EMEA)", "", True),
    ("Remote", "Applicants must be based in the US.", False),
    ("New York, NY", "", False),
    ("Remote", "", True),
])
def test_location(location, desc, ok):
    j = job(location=location, description=desc + " B2B SaaS fintech HubSpot GTM Series A")
    assert scoring.evaluate(j, SEARCH, TODAY).passed is ok


def test_stale_jobs_filtered():
    res = scoring.evaluate(job(posted_at="2026-08-01"), SEARCH, TODAY)
    assert not res.passed and "stale" in res.reasons[-1]


def test_keywords_raise_score():
    weak = scoring.evaluate(job(description="Consumer app"), SEARCH, TODAY)
    strong = scoring.evaluate(job(), SEARCH, TODAY)
    assert strong.score > weak.score and not weak.passed and strong.passed


# --- tracker -----------------------------------------------------------------

def test_tracker_funnel_and_followups(tmp_path):
    t = Tracker(tmp_path / "jobs.db")
    j = job()
    assert t.upsert(j, 60, ["x"], True, TODAY) is True
    assert t.upsert(j, 61, ["x"], True, TODAY) is False   # rescan doesn't duplicate
    t.mark(j.id, "applied", PROFILE["limits"], today=TODAY, note="via careers page")
    row = t.get(j.id[:6])
    assert row["status"] == "applied" and row["next_followup"] == "2026-10-04"
    assert t.upsert(j, 62, ["x"], True, TODAY) is False
    assert t.get(j.id)["status"] == "applied"               # rescan never resets funnel state
    assert t.due_followups(date(2026, 10, 3)) == []
    assert [r["id"] for r in t.due_followups(date(2026, 10, 4))] == [j.id]
    t.mark(j.id, "followup1", PROFILE["limits"], today=date(2026, 10, 4))
    assert t.get(j.id)["next_followup"] == "2026-10-11"
    assert t.count_today("applied", TODAY) == 1


def test_analysis_skip_moves_out_of_shortlist(tmp_path):
    t = Tracker(tmp_path / "jobs.db")
    j = job()
    t.upsert(j, 60, [], True, TODAY)
    t.save_analysis(j.id, {"fit_score": 20, "verdict": "skip"})
    assert t.shortlist() == []


# --- LLM layer (fake client) -------------------------------------------------

class FakeClient:
    def __init__(self, payload, stop_reason="end_turn"):
        self.calls = []
        self.payload, self.stop_reason = payload, stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_analyze_request_shape():
    payload = {"fit_score": 80, "verdict": "apply"}
    client = FakeClient(payload)
    assert llm.analyze(job(), PROFILE, client=client) == payload
    call = client.calls[0]
    assert call["model"] == llm.MODEL and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["schema"] is llm.ANALYSIS_SCHEMA
    assert "[ADD METRIC]" in call["system"]
    assert "Mirsis" in call["messages"][0]["content"]


def test_draft_truncates_connection_note():
    payload = {k: "x" for k in llm.DRAFTS_SCHEMA["required"]}
    payload["linkedin_connection_note"] = "word " * 100
    out = llm.draft(job(), PROFILE, client=FakeClient(payload))
    assert len(out["linkedin_connection_note"]) <= 300


def test_refusal_raises():
    with pytest.raises(llm.RefusalError):
        llm.analyze(job(), PROFILE, client=FakeClient({}, stop_reason="refusal"))


# --- LinkedIn URLs -----------------------------------------------------------

def test_linkedin_urls():
    urls = linkedin.search_urls(PROFILE["linkedin"])
    assert len(urls) == len(PROFILE["linkedin"]["keywords"]) * len(PROFILE["linkedin"]["locations"])
    assert "f_TPR=r86400" in urls[0][2] and "f_WT=2" in urls[0][2]
    assert linkedin.people_search_url("Acme", "Head of Growth").endswith("keywords=Head+of+Growth+Acme")
