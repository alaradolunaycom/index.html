"""Job sources with public, documented feeds.

Deliberately no LinkedIn scraping: LinkedIn's User Agreement bans bots and
scrapers, and a restricted account costs far more than any automation saves.
LinkedIn is covered by linkedin.py (search URLs you open yourself).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable, Iterable

import requests

from .models import Job, html_to_text

log = logging.getLogger(__name__)
TIMEOUT = 20
HEADERS = {"User-Agent": "job-agent/1.0 (personal job search)"}

Fetcher = Callable[[str], object]


def _get_json(url: str) -> object:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _get_text(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _iso(value: object) -> str:
    """Normalise epoch-ms, epoch-s or ISO strings to YYYY-MM-DD."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    try:  # RSS pubDate
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return text[:10]


def greenhouse(slug: str, get: Fetcher = _get_json) -> list[Job]:
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [
        Job(
            source="greenhouse",
            company=slug,
            title=j.get("title", ""),
            url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", ""),
            description=html_to_text(j.get("content")),
            posted_at=_iso(j.get("updated_at")),
        )
        for j in data.get("jobs", [])
    ]


def lever(slug: str, get: Fetcher = _get_json) -> list[Job]:
    data = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        workplace = (j.get("workplaceType") or "").lower()
        jobs.append(
            Job(
                source="lever",
                company=slug,
                title=j.get("text", ""),
                url=j.get("hostedUrl", ""),
                location=cats.get("location", "") or ", ".join(cats.get("allLocations") or []),
                description=j.get("descriptionPlain") or html_to_text(j.get("description")),
                posted_at=_iso(j.get("createdAt")),
                remote=True if workplace == "remote" else None,
            )
        )
    return jobs


def ashby(slug: str, get: Fetcher = _get_json) -> list[Job]:
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    jobs = []
    for j in data.get("jobs", []):
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        jobs.append(
            Job(
                source="ashby",
                company=slug,
                title=j.get("title", ""),
                url=j.get("jobUrl", ""),
                location=j.get("location", ""),
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                posted_at=_iso(j.get("publishedAt")),
                remote=j.get("isRemote"),
                salary=comp,
            )
        )
    return jobs


def remotive(category: str, get: Fetcher = _get_json) -> list[Job]:
    data = get(f"https://remotive.com/api/remote-jobs?category={category}")
    return [
        Job(
            source="remotive",
            company=j.get("company_name", ""),
            title=j.get("title", ""),
            url=j.get("url", ""),
            location=j.get("candidate_required_location", ""),
            description=html_to_text(j.get("description")),
            posted_at=_iso(j.get("publication_date")),
            remote=True,
            salary=j.get("salary") or "",
            tags=list(j.get("tags") or []),
        )
        for j in data.get("jobs", [])
    ]


def remoteok(tag: str, get: Fetcher = _get_json) -> list[Job]:
    data = get(f"https://remoteok.com/api?tag={tag}")
    jobs = []
    for j in data:
        if not isinstance(j, dict) or "position" not in j:  # first item is a legal notice
            continue
        salary = ""
        if j.get("salary_min") and j.get("salary_max"):
            salary = f"${j['salary_min']:,}–${j['salary_max']:,}"
        jobs.append(
            Job(
                source="remoteok",
                company=j.get("company", ""),
                title=j.get("position", ""),
                url=j.get("url", ""),
                location=j.get("location", "") or "Remote",
                description=html_to_text(j.get("description")),
                posted_at=_iso(j.get("date")),
                remote=True,
                salary=salary,
                tags=list(j.get("tags") or []),
            )
        )
    return jobs


def weworkremotely(feed_url: str, get: Callable[[str], str] = _get_text) -> list[Job]:
    root = ET.fromstring(get(feed_url))
    jobs = []
    for item in root.iter("item"):
        raw_title = item.findtext("title", "")
        company, _, title = raw_title.partition(": ")
        if not title:
            company, title = "", raw_title
        jobs.append(
            Job(
                source="weworkremotely",
                company=company.strip(),
                title=title.strip(),
                url=item.findtext("link", "").strip(),
                location=item.findtext("region", "") or "Remote",
                description=html_to_text(item.findtext("description", "")),
                posted_at=_iso(item.findtext("pubDate")),
                remote=True,
            )
        )
    return jobs


def collect(config: dict) -> tuple[list[Job], list[str]]:
    """Run every configured source. Returns (jobs, errors); one bad source never stops the scan."""
    tasks: list[tuple[str, Callable[[], Iterable[Job]]]] = []
    companies = config.get("companies") or {}
    for slug in companies.get("greenhouse") or []:
        tasks.append((f"greenhouse:{slug}", lambda s=slug: greenhouse(s)))
    for slug in companies.get("lever") or []:
        tasks.append((f"lever:{slug}", lambda s=slug: lever(s)))
    for slug in companies.get("ashby") or []:
        tasks.append((f"ashby:{slug}", lambda s=slug: ashby(s)))
    agg = config.get("aggregators") or {}
    for cat in agg.get("remotive_categories") or []:
        tasks.append((f"remotive:{cat}", lambda c=cat: remotive(c)))
    for tag in agg.get("remoteok_tags") or []:
        tasks.append((f"remoteok:{tag}", lambda t=tag: remoteok(t)))
    for feed in agg.get("weworkremotely_feeds") or []:
        tasks.append((f"wwr:{feed}", lambda f=feed: weworkremotely(f)))

    jobs: list[Job] = []
    errors: list[str] = []
    seen: set[str] = set()
    for name, task in tasks:
        try:
            batch = list(task())
        except Exception as exc:  # network, 404 slug, bad JSON — log and move on
            errors.append(f"{name}: {exc}")
            log.warning("source %s failed: %s", name, exc)
            continue
        for job in batch:
            if job.url and job.id not in seen:
                seen.add(job.id)
                jobs.append(job)
        log.info("source %s: %d jobs", name, len(batch))
    return jobs, errors
