"""LinkedIn helpers that stay inside LinkedIn's rules: build URLs, never log in or scrape."""
from __future__ import annotations

from urllib.parse import urlencode

JOBS = "https://www.linkedin.com/jobs/search/"
PEOPLE = "https://www.linkedin.com/search/results/people/"


def job_search_url(keywords: str, location: str, remote_only: bool = True,
                   posted_within_hours: int = 24) -> str:
    params = {
        "keywords": keywords,
        "location": location,
        "f_TPR": f"r{posted_within_hours * 3600}",
        "sortBy": "DD",
    }
    if remote_only:
        params["f_WT"] = "2"
    return f"{JOBS}?{urlencode(params)}"


def search_urls(cfg: dict) -> list[tuple[str, str, str]]:
    """(keywords, location, url) for every configured combination."""
    return [
        (kw, loc, job_search_url(kw, loc, cfg.get("remote_only", True), cfg.get("posted_within_hours", 24)))
        for kw in cfg.get("keywords", [])
        for loc in cfg.get("locations", [])
    ]


def people_search_url(company: str, title: str) -> str:
    return f"{PEOPLE}?{urlencode({'keywords': f'{title} {company}'})}"
