"""Deterministic pre-filter: cheap, explainable, runs on every job before any LLM call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import Job


@dataclass
class RuleResult:
    passed: bool
    score: int
    reasons: list[str] = field(default_factory=list)


def _has(term: str, text: str) -> bool:
    # Word-boundary match so "vp" doesn't hit "mvp" and "gtm" doesn't hit "gtmx".
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _location_ok(job: Job, search: dict) -> tuple[bool, str]:
    loc = f"{job.location} {job.title}".lower()
    desc = job.description.lower()
    for phrase in search.get("location_block", []):
        if phrase in loc or phrase in desc:
            return False, f"location blocked: '{phrase}'"
    if not job.location.strip() and job.remote:
        return True, "remote, no restriction listed"
    specific = [ok for ok in search.get("location_ok", []) if ok != "remote" and _has(ok, loc)]
    if specific:
        return True, f"location ok: '{specific[0]}'"
    # "Remote - US" style: generic remote, but tied to a country we can't work from.
    foreign = [f for f in search.get("location_foreign", []) if _has(f, loc)]
    if foreign:
        return False, f"location outside target regions: '{job.location}'"
    if "remote" in search.get("location_ok", []) and _has("remote", loc):
        return True, "location ok: 'remote'"
    return False, f"location not in target regions: '{job.location}'"


def evaluate(job: Job, search: dict, today: date | None = None) -> RuleResult:
    title = job.title.lower()
    reasons: list[str] = []

    blocked = [t for t in search.get("title_exclude", []) if _has(t, title)]
    if blocked:
        return RuleResult(False, 0, [f"title excluded: {blocked[0]}"])
    matched = [t for t in search.get("title_include", []) if _has(t, title)]
    if not matched:
        return RuleResult(False, 0, ["title not in target lane"])
    reasons.append(f"title match: {matched[0]}")

    ok, why = _location_ok(job, search)
    if not ok:
        return RuleResult(False, 0, reasons + [why])
    reasons.append(why)

    max_age = search.get("max_age_days")
    if max_age and job.posted_at:
        try:
            posted = date.fromisoformat(job.posted_at[:10])
        except ValueError:
            posted = None
        if posted and posted < (today or date.today()) - timedelta(days=max_age):
            return RuleResult(False, 0, reasons + [f"stale: posted {job.posted_at}"])

    score = 20 + 5 * min(len(matched), 3)
    text = f"{title}\n{job.description.lower()}"
    hits = [k for k in search.get("keyword_weights", {}) if _has(k, text)]
    score += sum(search["keyword_weights"][k] for k in hits)
    if hits:
        reasons.append("keywords: " + ", ".join(hits[:8]))
    score = min(score, 100)
    return RuleResult(score >= search.get("shortlist_min_rule_score", 0), score, reasons)
