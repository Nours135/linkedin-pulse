"""Tool: generate search queries and filter keywords from the resume, keyword pre-filter,
JD + resume match score."""

from __future__ import annotations

import hashlib
import json
import re
from fnmatch import fnmatchcase
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field

from pulse.candidates import Candidate
from pulse.config import ROOT
from pulse.llm import llm
from pulse.tools import latex
from pulse.tools.linkedin import Job


class Keywords(BaseModel):
    search_queries: list[str] = Field(description="LinkedIn job search queries: short job-title phrases for roles that fit")
    include: list[str] = Field(description="lowercase skills, technologies and domains a matching JD would mention")
    exclude_title: list[str] = Field(description="lowercase job-title words that mean the role is a bad fit")


class Match(BaseModel):
    score: int = Field(description="0-100 fit score")
    reasons: list[str] = Field(description="2-4 short sentences explaining the score")
    matched: list[str] = Field(description="required skills the candidate has")
    missing: list[str] = Field(description="required skills the candidate lacks")
    job_type: Literal["new_grad", "summer_intern", "intern", "part_time", "other"] = Field(
        description="new_grad: entry-level role for recent/upcoming graduates; summer_intern: summer "
                    "internship; intern: other internship or co-op; part_time: part-time role; other: none of these")
    cohort_year: int | None = Field(
        description="graduation or internship year the posting targets, e.g. 2027; null if not stated")
    work_auth: Literal["citizenship_required", "no_sponsorship", "not_stated"] = Field(
        description="citizenship_required: requires US citizenship (incl. clearance that needs it); "
                    "no_sponsorship: will not sponsor visas; not_stated: neither is stated explicitly")
    work_auth_quote: str = Field(description="the posting's exact sentence behind work_auth; empty if not_stated")

    def tags(self) -> list[str]:
        """Short labels for the email body, logs and tag filter, e.g. ["27NG", "NoSponsor"]."""
        yy = f"{self.cohort_year % 100:02d}" if self.cohort_year else ""
        kind = {"new_grad": f"{yy}NG", "summer_intern": f"{yy}Summer", "intern": f"{yy}Intern",
                "part_time": "PartTime"}.get(self.job_type)
        auth = {"citizenship_required": "CitizenOnly", "no_sponsorship": "NoSponsor"}.get(self.work_auth)
        return [t for t in (kind, auth) if t]


KEYWORDS_PROMPT = """\
Below is a candidate's resume: a catalog of experiences and projects plus a skills section.

1. search_queries: list 6-8 LinkedIn job search queries, each a short job-title phrase at the
   candidate's level, e.g. "Software Engineer New Grad", "Backend Engineer", "Machine Learning
   Engineer". Cover the directions the resume is strongest in first, then adjacent titles that
   hire the same profile under another name (e.g. "Research Engineer" or "Software Engineer, ML"
   next to "Machine Learning Engineer"). Each query runs its own search, so no near-duplicates.
2. include: list 25-40 keywords (technologies, skills, domains) that a job description fitting
   this candidate would likely mention. Lowercase, one concept each, e.g. "python", "kubernetes",
   "distributed systems", "llm".
3. exclude_title: list words that, when they appear in a job title, mean the role is clearly out
   of reach for this candidate's experience level or field, e.g. "staff", "principal", "director".

Resume:
{resume}
"""

SCORE_PROMPT = """\
Score how well this candidate fits the job, from 0 to 100.

- 90-100: meets nearly all required qualifications and the level matches.
- 70-89: meets most required qualifications; the gaps are learnable on the job.
- 40-69: partial overlap; important required skills or the expected level are missing.
- 0-39: different field, or requirements clearly out of reach (e.g. many more years of experience).

Judge required qualifications more heavily than nice-to-haves. A required security clearance,
US citizenship requirement, or "no visa sponsorship" is out of reach unless the resume shows the
candidate meets it: cap the score at 39 and name the requirement in the reasons.

Also classify the posting:
- job_type and cohort_year: e.g. "Software Engineer, New Grad 2027" is new_grad / 2027 and
  "Summer 2027 SWE Intern" is summer_intern / 2027. Use the year the posting targets, not the
  posting date; null if no year is stated.
- work_auth: only what the posting says explicitly. If it both requires citizenship and refuses
  sponsorship, use citizenship_required. Equal-opportunity boilerplate or "must be authorized to
  work in the US" alone is not_stated. Quote the sentence in work_auth_quote.

Job: {title} at {company} ({location})
Description:
{description}

Candidate resume:
{resume}
"""


@lru_cache
def resume_text(cand: Candidate) -> str:
    return latex.resume_text(cand)


def generate_keywords(cand: Candidate) -> Keywords:
    """LinkedIn search queries plus the title-filter and JD pre-filter keywords for one candidate.
    Generated by the LLM on first use and saved to work/<candidate>/keywords.json, which may be
    hand-edited (read again every heartbeat); it is regenerated, losing hand edits, only when the
    resume text or KEYWORDS_PROMPT changes, or the file lacks a field."""
    path = cand.work_dir / "keywords.json"
    # resume_sha also covers the prompt, so a prompt change regenerates the keywords
    digest = hashlib.sha256((KEYWORDS_PROMPT + resume_text(cand)).encode()).hexdigest()[:16]
    if path.exists():
        saved = json.loads(path.read_text())
        missing = set(Keywords.model_fields) - saved.keys()
        if saved.get("resume_sha") == digest and not missing:
            return Keywords.model_validate(saved)
        why = f"missing {', '.join(sorted(missing))}" if missing else "resume or prompt changed"
        print(f"[keywords] {path.relative_to(ROOT)}: {why}; regenerating")

    kw = llm.parse(KEYWORDS_PROMPT.format(resume=resume_text(cand)), Keywords, tag="match.keywords")
    kw.search_queries = [q.strip() for q in kw.search_queries if q.strip()]
    kw.include = [k.lower().strip() for k in kw.include if k.strip()]
    kw.exclude_title = [k.lower().strip() for k in kw.exclude_title if k.strip()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"resume_sha": digest, **kw.model_dump()}, ensure_ascii=False, indent=2))
    return kw


def _contains(text: str, word: str) -> bool:
    # word boundaries so "go" does not match "google" and "c" does not match everything
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def title_excluded(title: str, kw: Keywords) -> bool:
    """A title like "Software Engineer/Sr. Software Engineer" hires at either level, so it is
    excluded only if every "/"-separated alternative is."""
    parts = [p for p in title.lower().split("/") if p.strip()] or [title.lower()]
    return all(any(_contains(p, w) for w in kw.exclude_title) for p in parts)


def keyword_hits(text: str, kw: Keywords) -> list[str]:
    text = text.lower()
    return [k for k in kw.include if _contains(text, k)]


def tag_rejected(tags: list[str], include: tuple[str, ...], exclude: tuple[str, ...]) -> str | None:
    """Why a job with these tags is filtered out, or None to keep it. Patterns are case-insensitive
    globs, so "*NG" matches any new-grad year. include: keep only jobs with a tag matching one of
    them (empty keeps all); exclude: drop jobs with a tag matching any of them."""
    def hit(patterns: tuple[str, ...]) -> list[str]:
        return [t for t in tags if any(fnmatchcase(t.lower(), p.lower()) for p in patterns)]
    if bad := hit(exclude):
        return f"tags {', '.join(bad)} excluded"
    if include and not hit(include):
        return f"tags [{', '.join(tags)}] match none of include"
    return None


def score_match(job: Job, cand: Candidate) -> Match:
    m = llm.parse(SCORE_PROMPT.format(
        title=job.title, company=job.company, location=job.location,
        description=job.description, resume=resume_text(cand),
    ), Match, tag="match.score")
    m.score = max(0, min(100, m.score))
    return m
