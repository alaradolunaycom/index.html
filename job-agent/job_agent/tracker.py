"""SQLite pipeline: every job, its scores, drafts and where it sits in the funnel."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .models import Job

STATUSES = ("new", "shortlisted", "skipped", "applied", "outreach", "followup1", "followup2",
            "interview", "offer", "rejected", "closed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT, company TEXT, title TEXT, location TEXT, url TEXT,
    description TEXT, posted_at TEXT, salary TEXT,
    rule_score INTEGER, rule_reasons TEXT,
    fit_score INTEGER, verdict TEXT, analysis TEXT, drafts TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen TEXT, applied_at TEXT, outreach_at TEXT, next_followup TEXT,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    job_id TEXT, day TEXT, kind TEXT
);
"""


class Tracker:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def upsert(self, job: Job, rule_score: int, reasons: list[str], passed: bool,
               today: date | None = None) -> bool:
        """Insert a new job or refresh its scraped fields. Never touches funnel state. Returns True if new."""
        today = today or date.today()
        exists = self.db.execute("SELECT 1 FROM jobs WHERE id=?", (job.id,)).fetchone()
        if exists:
            self.db.execute(
                "UPDATE jobs SET description=?, location=?, salary=?, rule_score=?, rule_reasons=? WHERE id=?",
                (job.description, job.location, job.salary, rule_score, json.dumps(reasons), job.id),
            )
        else:
            self.db.execute(
                "INSERT INTO jobs (id, source, company, title, location, url, description, posted_at, salary,"
                " rule_score, rule_reasons, status, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job.id, job.source, job.company, job.title, job.location, job.url, job.description,
                 job.posted_at, job.salary, rule_score, json.dumps(reasons),
                 "shortlisted" if passed else "skipped", today.isoformat()),
            )
        self.db.commit()
        return not exists

    def save_analysis(self, job_id: str, analysis: dict) -> None:
        status = "skipped" if analysis.get("verdict") == "skip" else "shortlisted"
        self.db.execute(
            "UPDATE jobs SET fit_score=?, verdict=?, analysis=?, status=CASE WHEN status IN ('new','shortlisted')"
            " THEN ? ELSE status END WHERE id=?",
            (analysis.get("fit_score"), analysis.get("verdict"), json.dumps(analysis, ensure_ascii=False),
             status, job_id),
        )
        self.db.commit()

    def save_drafts(self, job_id: str, drafts: dict) -> None:
        self.db.execute("UPDATE jobs SET drafts=? WHERE id=?", (json.dumps(drafts, ensure_ascii=False), job_id))
        self.db.commit()

    def get(self, job_id: str) -> sqlite3.Row | None:
        rows = self.db.execute("SELECT * FROM jobs WHERE id LIKE ?", (job_id + "%",)).fetchall()
        return rows[0] if len(rows) == 1 else None

    def needs_analysis(self, limit: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='shortlisted' AND fit_score IS NULL"
            " ORDER BY rule_score DESC LIMIT ?", (limit,)).fetchall()

    def shortlist(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='shortlisted'"
            " ORDER BY COALESCE(fit_score, rule_score) DESC, first_seen DESC LIMIT ?", (limit,)).fetchall()

    def mark(self, job_id: str, status: str, cfg_limits: dict, today: date | None = None,
             note: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        today = today or date.today()
        first = cfg_limits.get("followup_after_days", 5)
        second = cfg_limits.get("second_followup_after_days", 7)
        fields: dict[str, object] = {"status": status}
        if status == "applied":
            fields["applied_at"] = today.isoformat()
            fields["next_followup"] = (today + timedelta(days=first)).isoformat()
        elif status == "outreach":
            fields["outreach_at"] = today.isoformat()
            fields["next_followup"] = (today + timedelta(days=first)).isoformat()
        elif status == "followup1":
            fields["next_followup"] = (today + timedelta(days=second)).isoformat()
        else:
            fields["next_followup"] = None
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values())
        if note:
            sets += ", notes = COALESCE(notes,'') || ?"
            params.append(f"[{today.isoformat()}] {note}\n")
        self.db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*params, job_id))
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (job_id, today.isoformat(), status))
        self.db.commit()

    def due_followups(self, today: date | None = None) -> list[sqlite3.Row]:
        today = today or date.today()
        return self.db.execute(
            "SELECT * FROM jobs WHERE next_followup IS NOT NULL AND next_followup <= ?"
            " AND status IN ('applied','outreach','followup1') ORDER BY next_followup",
            (today.isoformat(),)).fetchall()

    def count_today(self, kind: str, today: date | None = None) -> int:
        today = today or date.today()
        return self.db.execute("SELECT COUNT(*) FROM events WHERE day=? AND kind=?",
                               (today.isoformat(), kind)).fetchone()[0]

    def funnel(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def all_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM jobs ORDER BY first_seen DESC").fetchall()
