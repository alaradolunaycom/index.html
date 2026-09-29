"""job-agent CLI.

  python -m job_agent scan            # fetch feeds, filter, (Claude) screen, write digest
  python -m job_agent add URL ...     # add a job you found on LinkedIn (paste JD from stdin/file)
  python -m job_agent list            # shortlist, best first
  python -m job_agent draft ID        # cover note + LinkedIn note + DM + follow-ups
  python -m job_agent mark ID applied # move through the funnel (sets follow-up dates)
  python -m job_agent followups       # what to chase today
  python -m job_agent linkedin        # LinkedIn search URLs for today's manual sweep
  python -m job_agent export          # CSV of the whole pipeline
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import yaml

from . import linkedin, llm, scoring, sources
from .models import Job
from .tracker import STATUSES, Tracker

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "profile.yaml"
DATA = Path(os.environ.get("JOB_AGENT_DATA", ROOT / "data"))


def load_profile(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _row_job(row) -> Job:
    return Job(source=row["source"], company=row["company"], title=row["title"], url=row["url"],
               location=row["location"] or "", description=row["description"] or "",
               posted_at=row["posted_at"] or "", salary=row["salary"] or "")


def _resolve(tracker: Tracker, job_id: str):
    row = tracker.get(job_id)
    if row is None:
        sys.exit(f"No unique job matches id '{job_id}'. Run `list` to see ids.")
    return row


def _screen(tracker: Tracker, profile: dict, rows) -> int:
    done = 0
    for row in rows:
        try:
            analysis = llm.analyze(_row_job(row), profile)
        except Exception as exc:
            print(f"  ! screening failed for {row['id']} ({row['company']}): {exc}")
            continue
        tracker.save_analysis(row["id"], analysis)
        done += 1
        print(f"  {row['id']}  {analysis['fit_score']:>3}  {analysis['verdict']:<7} {row['company']} — {row['title']}")
    return done


def cmd_scan(args, profile, tracker):
    search = profile["search"]
    jobs, errors = sources.collect(profile)
    new = passed = 0
    for job in jobs:
        res = scoring.evaluate(job, search)
        passed += res.passed
        new += tracker.upsert(job, res.score, res.reasons, res.passed)
    print(f"Fetched {len(jobs)} jobs · {new} new · {passed} pass the rule filter · {len(errors)} source errors")
    for err in errors:
        print(f"  ! {err}")
    if args.no_llm or not llm_available():
        if not args.no_llm:
            print("ANTHROPIC_API_KEY not set — skipping Claude screening (rule scores only).")
    else:
        rows = tracker.needs_analysis(search.get("max_llm_reviews_per_run", 25))
        print(f"Screening {len(rows)} shortlisted jobs with Claude…")
        _screen(tracker, profile, rows)
    path = write_digest(profile, tracker)
    print(f"Digest → {path}")


def cmd_add(args, profile, tracker):
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    job = Job(source=args.source, company=args.company, title=args.title, url=args.url,
              location=args.location, description=text.strip())
    res = scoring.evaluate(job, profile["search"])
    # A job you picked by hand is shortlisted even if the rules disagree; the reasons are shown.
    tracker.upsert(job, res.score, res.reasons, True)
    print(f"Added {job.id}  rule score {res.score}  ({'; '.join(res.reasons)})")
    if llm_available() and not args.no_llm:
        _screen(tracker, profile, [tracker.get(job.id)])


def cmd_list(args, profile, tracker):
    rows = tracker.shortlist(args.limit)
    if not rows:
        print("Shortlist is empty. Run `scan` or `add`.")
    for r in rows:
        score = r["fit_score"] if r["fit_score"] is not None else f"~{r['rule_score']}"
        print(f"{r['id']}  {str(score):>4}  {(r['verdict'] or '-'):<7} {r['company'][:22]:<22} {r['title'][:50]:<50} {r['location'][:25]}")


def cmd_show(args, profile, tracker):
    r = _resolve(tracker, args.id)
    print(f"{r['title']} — {r['company']}\n{r['location']} · {r['source']} · posted {r['posted_at'] or '?'}\n{r['url']}\n")
    print("Rule:", ", ".join(json.loads(r["rule_reasons"] or "[]")))
    if r["analysis"]:
        print(json.dumps(json.loads(r["analysis"]), indent=2, ensure_ascii=False))
    if r["drafts"]:
        print(render_drafts(r, json.loads(r["drafts"])))
    print(f"\nStatus: {r['status']}  next follow-up: {r['next_followup'] or '-'}\n{r['notes'] or ''}")


def render_drafts(row, d: dict) -> str:
    analysis = json.loads(row["analysis"] or "{}")
    contacts = analysis.get("contact_titles") or ["Head of Marketing", "Head of Growth"]
    people = "\n".join(f"- {t}: {linkedin.people_search_url(row['company'], t)}" for t in contacts[:3])
    bullets = lambda xs: "\n".join(f"- {x}" for x in xs)  # noqa: E731
    return f"""
## {row['title']} — {row['company']}
{row['url']}

### CV emphasis (reorder/reword only)
{bullets(d['cv_emphasis'])}

### Cover note
{d['cover_note']}

### Who to contact (LinkedIn people search)
{people}

### LinkedIn connection note ({len(d['linkedin_connection_note'])}/300)
{d['linkedin_connection_note']}

### Hiring manager message
{d['hiring_manager_message']}

### Referral ask (current employee)
{d['referral_ask']}

### Follow-up 1 (day +5)
{d['followup_1']}

### Follow-up 2 (day +12)
{d['followup_2']}

### Interview prep
{bullets(d['interview_prep'])}
"""


def cmd_draft(args, profile, tracker):
    if not llm_available():
        sys.exit("Drafting needs ANTHROPIC_API_KEY (see README).")
    r = _resolve(tracker, args.id)
    analysis = json.loads(r["analysis"]) if r["analysis"] else None
    if analysis is None:
        analysis = llm.analyze(_row_job(r), profile)
        tracker.save_analysis(r["id"], analysis)
    if analysis.get("verdict") == "skip" and not args.force:
        sys.exit(f"Screening says skip ({'; '.join(analysis.get('dealbreakers') or analysis.get('gaps') or [])}). "
                 "Use --force if you disagree.")
    drafts = llm.draft(_row_job(r), profile, analysis)
    tracker.save_drafts(r["id"], drafts)
    text = render_drafts(tracker.get(r["id"]), drafts)
    out = DATA / "drafts" / f"{date.today().isoformat()}-{r['id']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"Saved → {out}\nReview every line, fill any [ADD METRIC], then send it yourself.")


def cmd_mark(args, profile, tracker):
    r = _resolve(tracker, args.id)
    limits = profile.get("limits", {})
    cap_key = {"applied": "daily_applications", "outreach": "daily_outreach"}.get(args.status)
    if cap_key:
        done = tracker.count_today(args.status)
        if done >= limits.get(cap_key, 10**6):
            print(f"Heads-up: {done} '{args.status}' today already (cap {limits[cap_key]}). "
                  "Quality over volume — consider stopping here.")
    tracker.mark(r["id"], args.status, limits, note=args.note or "")
    r = tracker.get(r["id"])
    print(f"{r['id']} → {r['status']}  next follow-up: {r['next_followup'] or '-'}")


def cmd_followups(args, profile, tracker):
    rows = tracker.due_followups()
    if not rows:
        print("No follow-ups due today.")
    for r in rows:
        which = "followup_2" if r["status"] == "followup1" else "followup_1"
        print(f"{r['id']}  due {r['next_followup']}  [{r['status']}] {r['company']} — {r['title']}")
        if r["drafts"]:
            print("   " + json.loads(r["drafts"])[which].replace("\n", "\n   "))
        next_status = "followup2" if r["status"] == "followup1" else "followup1"
        print(f"   after sending: python -m job_agent mark {r['id']} {next_status}\n")


def cmd_linkedin(args, profile, tracker):
    for kw, loc, url in linkedin.search_urls(profile.get("linkedin", {})):
        print(f"{kw} · {loc}\n  {url}")


def cmd_export(args, profile, tracker):
    out = Path(args.out)
    cols = ["id", "status", "fit_score", "verdict", "rule_score", "company", "title", "location", "url",
            "posted_at", "first_seen", "applied_at", "outreach_at", "next_followup", "notes"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in tracker.all_rows():
            w.writerow([r[c] for c in cols])
    print(f"Exported → {out}")


def write_digest(profile: dict, tracker: Tracker, today: date | None = None) -> Path:
    today = today or date.today()
    limits = profile.get("limits", {})
    lines = [f"# Job digest — {today.isoformat()}", ""]
    funnel = tracker.funnel()
    lines.append("**Funnel:** " + " · ".join(f"{s} {funnel[s]}" for s in STATUSES if funnel.get(s)))
    lines.append(f"**Today's budget:** {limits.get('daily_applications', '?')} applications, "
                 f"{limits.get('daily_outreach', '?')} outreach messages\n")
    due = tracker.due_followups(today)
    if due:
        lines.append("## Follow-ups due")
        lines += [f"- `{r['id']}` {r['company']} — {r['title']} (due {r['next_followup']})" for r in due]
        lines.append("")
    lines.append("## Shortlist (best first)")
    lines.append("| id | fit | verdict | company | role | location | angle |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in tracker.shortlist(30):
        a = json.loads(r["analysis"] or "{}")
        fit = r["fit_score"] if r["fit_score"] is not None else f"~{r['rule_score']}"
        angle = (a.get("positioning_angle") or "").replace("|", "/")
        lines.append(f"| `{r['id']}` | {fit} | {r['verdict'] or '-'} | {r['company']} | "
                     f"[{r['title']}]({r['url']}) | {r['location']} | {angle} |")
    lines.append("\n## LinkedIn sweep (open, filter, `add` the good ones)")
    lines += [f"- [{kw} · {loc}]({url})" for kw, loc, url in linkedin.search_urls(profile.get("linkedin", {}))]
    path = DATA / f"digest-{today.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="job_agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=str(DEFAULT_PROFILE))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan"); s.add_argument("--no-llm", action="store_true"); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("add")
    s.add_argument("url"); s.add_argument("--title", required=True); s.add_argument("--company", required=True)
    s.add_argument("--location", default=""); s.add_argument("--file", help="job description text file (default: stdin)")
    s.add_argument("--source", default="linkedin"); s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("list"); s.add_argument("--limit", type=int, default=30); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("show"); s.add_argument("id"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("draft"); s.add_argument("id"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_draft)
    s = sub.add_parser("mark"); s.add_argument("id"); s.add_argument("status", choices=STATUSES)
    s.add_argument("--note"); s.set_defaults(fn=cmd_mark)
    s = sub.add_parser("followups"); s.set_defaults(fn=cmd_followups)
    s = sub.add_parser("linkedin"); s.set_defaults(fn=cmd_linkedin)
    s = sub.add_parser("export"); s.add_argument("--out", default=str(DATA / "pipeline.csv"))
    s.set_defaults(fn=cmd_export)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    profile = load_profile(Path(args.profile))
    tracker = Tracker(DATA / "jobs.db")
    args.fn(args, profile, tracker)


if __name__ == "__main__":
    main()
