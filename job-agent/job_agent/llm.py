"""Claude-powered fit analysis and outreach drafting (structured JSON outputs)."""
from __future__ import annotations

import json
import os

import yaml

from .models import Job

MODEL = os.environ.get("JOB_AGENT_MODEL", "claude-opus-5-5")

GROUND_RULES = """\
Ground rules (non-negotiable):
- Use ONLY facts present in the candidate profile. Never invent employers, titles, dates,
  metrics, clients, tools or results.
- Where a number would make a claim credible but the profile has none, write the literal
  placeholder [ADD METRIC] so the candidate fills it in. Do not guess a number.
- Be honest about gaps. A recruiter spots inflated fit in seconds; credibility is the asset.
- Plain, specific, human English. No buzzword stacks, no "I hope this finds you well",
  no "passionate", no emojis.
"""

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "integer", "description": "0-100 likelihood this candidate gets an interview"},
        "verdict": {"type": "string", "enum": ["apply", "stretch", "skip"]},
        "seniority_match": {"type": "string", "enum": ["under", "match", "over"]},
        "location_eligible": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "top_reasons": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "dealbreakers": {"type": "array", "items": {"type": "string"}},
        "positioning_angle": {"type": "string", "description": "One sentence: the single strongest story to lead with"},
        "contact_titles": {"type": "array", "items": {"type": "string"},
                            "description": "Job titles of the likely hiring manager / team lead to contact"},
    },
    "required": ["fit_score", "verdict", "seniority_match", "location_eligible", "top_reasons",
                 "gaps", "dealbreakers", "positioning_angle", "contact_titles"],
    "additionalProperties": False,
}

DRAFTS_SCHEMA = {
    "type": "object",
    "properties": {
        "cv_emphasis": {"type": "array", "items": {"type": "string"},
                        "description": "3-5 existing CV bullets to move up or reword for this role (reword only, no new facts)"},
        "cover_note": {"type": "string", "description": "Max 150 words. For the application form's cover letter / 'why us' field"},
        "linkedin_connection_note": {"type": "string", "description": "Max 280 characters. To the hiring manager"},
        "hiring_manager_message": {"type": "string", "description": "Max 120 words. LinkedIn DM or email after connecting"},
        "referral_ask": {"type": "string", "description": "Max 90 words. To a current employee (not the hiring manager) asking for a referral"},
        "followup_1": {"type": "string", "description": "Max 60 words, sent ~5 days after no reply, adds one new piece of value"},
        "followup_2": {"type": "string", "description": "Max 40 words, final polite close-the-loop"},
        "interview_prep": {"type": "array", "items": {"type": "string"},
                           "description": "3 likely interview questions for this role with a one-line answer hook each"},
    },
    "required": ["cv_emphasis", "cover_note", "linkedin_connection_note", "hiring_manager_message",
                 "referral_ask", "followup_1", "followup_2", "interview_prep"],
    "additionalProperties": False,
}


class RefusalError(RuntimeError):
    pass


def _client():
    import anthropic  # imported lazily so rule-only runs don't need the SDK

    return anthropic.Anthropic()


def _profile_block(profile: dict) -> str:
    return yaml.safe_dump(profile["candidate"], allow_unicode=True, sort_keys=False)


def _job_block(job: Job, limit: int = 12000) -> str:
    desc = job.description if len(job.description) <= limit else job.description[:limit] + "\n[...]"
    return (
        f"Company: {job.company}\nTitle: {job.title}\nLocation: {job.location}\n"
        f"Salary: {job.salary or 'not listed'}\nURL: {job.url}\n\nDescription:\n{desc}"
    )


def _structured_call(system: str, user: str, schema: dict, effort: str, client=None) -> dict:
    client = client or _client()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        raise RefusalError("model declined the request")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("response truncated (max_tokens)")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def analyze(job: Job, profile: dict, client=None) -> dict:
    system = (
        "You are a blunt senior tech recruiter screening roles for one candidate. "
        "Score how likely she is to get an interview, not how nice the job sounds. "
        "Weigh: title/seniority fit, B2B SaaS/fintech/early-stage overlap, whether the "
        "company can legally and practically hire someone based in Türkiye, language "
        "requirements, and must-have skills the candidate lacks.\n\n" + GROUND_RULES
    )
    user = f"<candidate>\n{_profile_block(profile)}</candidate>\n\n<job>\n{_job_block(job)}\n</job>"
    return _structured_call(system, user, ANALYSIS_SCHEMA, effort="medium", client=client)


def draft(job: Job, profile: dict, analysis: dict | None = None, client=None) -> dict:
    system = (
        "You write job-application and outreach copy for one candidate. Each message must "
        "reference something specific from the job post or company, lead with the single "
        "most relevant proof point, and end with one low-friction ask. Messages to people "
        "are short enough to read on a phone.\n\n" + GROUND_RULES
    )
    user = (
        f"<candidate>\n{_profile_block(profile)}</candidate>\n\n<job>\n{_job_block(job)}\n</job>\n\n"
        f"<screening>\n{json.dumps(analysis or {}, ensure_ascii=False, indent=2)}\n</screening>"
    )
    result = _structured_call(system, user, DRAFTS_SCHEMA, effort="high", client=client)
    note = result.get("linkedin_connection_note", "")
    if len(note) > 300:  # LinkedIn hard limit on connection notes
        result["linkedin_connection_note"] = note[:297].rsplit(" ", 1)[0] + "…"
    return result
