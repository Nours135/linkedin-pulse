"""Tool: generate keywords from the resume, keyword pre-filter, JD + resume match score."""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import BaseModel, Field

from pulse.agents.resume_agent import format_catalog
from pulse.config import load_resume
from pulse.llm import llm
from pulse.tools.latex import COMPONENTS_DIR
from pulse.tools.linkedin import Job


class Keywords(BaseModel):
    include: list[str] = Field(description="lowercase skills, technologies and domains a matching JD would mention")
    exclude_title: list[str] = Field(description="lowercase job-title words that mean the role is a bad fit")


class Match(BaseModel):
    score: int = Field(description="0-100 fit score")
    reasons: list[str] = Field(description="2-4 short sentences explaining the score")
    matched: list[str] = Field(description="required skills the candidate has")
    missing: list[str] = Field(description="required skills the candidate lacks")


KEYWORDS_PROMPT = """\
Below is a candidate's resume: a catalog of experiences and projects plus a skills section.

1. include: list 25-40 keywords (technologies, skills, domains) that a job description fitting
   this candidate would likely mention. Lowercase, one concept each, e.g. "python", "kubernetes",
   "distributed systems", "llm".
2. exclude_title: list words that, when they appear in a job title, mean the role is clearly out
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

Judge required qualifications more heavily than nice-to-haves.

Job: {title} at {company} ({location})
Description:
{description}

Candidate resume:
{resume}
"""


def _ask(prompt: str, output_format: type[BaseModel]) -> BaseModel:
    extra = {}
    if not llm.model.startswith("claude-haiku"):  # Haiku has no server-side fallback
        # retry safety-classifier declines on Anthropic's recommended fallback model
        extra = {"extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
                 "extra_body": {"fallbacks": "default"}}
    resp = llm.client.messages.parse(
        model=llm.model,
        max_tokens=llm.max_tokens,
        messages=[{"role": "user", "content": prompt}],
        output_format=output_format,
        **extra,
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"no structured output from LLM (stop_reason={resp.stop_reason})")
    return resp.parsed_output


@lru_cache
def resume_text() -> str:
    return f"{format_catalog(load_resume())}\n\n[Skills]\n{(COMPONENTS_DIR / 'skills.tex').read_text()}"


@lru_cache
def generate_keywords() -> Keywords:
    """Generated once per process; the resume does not change between heartbeats."""
    kw = _ask(KEYWORDS_PROMPT.format(resume=resume_text()), Keywords)
    kw.include = [k.lower().strip() for k in kw.include if k.strip()]
    kw.exclude_title = [k.lower().strip() for k in kw.exclude_title if k.strip()]
    return kw


def _contains(text: str, word: str) -> bool:
    # word boundaries so "go" does not match "google" and "c" does not match everything
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def title_excluded(title: str, kw: Keywords) -> bool:
    title = title.lower()
    return any(_contains(title, w) for w in kw.exclude_title)


def keyword_hits(text: str, kw: Keywords) -> list[str]:
    text = text.lower()
    return [k for k in kw.include if _contains(text, k)]


def score_match(job: Job) -> Match:
    m = _ask(SCORE_PROMPT.format(
        title=job.title, company=job.company, location=job.location,
        description=job.description, resume=resume_text(),
    ), Match)
    m.score = max(0, min(100, m.score))
    return m
