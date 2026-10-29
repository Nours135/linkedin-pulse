"""Shared LangGraph State definition (new jobs, processing results, etc.)."""

from __future__ import annotations

from typing import TypedDict

from pulse.candidates import Candidate
from pulse.tools.linkedin import Job


class JobResult(TypedDict, total=False):
    candidate: str       # candidate slug
    job_id: str
    title: str
    company: str
    url: str
    status: str          # filtered | low_score | sent | error
    reason: str          # why it was filtered
    score: int
    pdf: str | None
    sent_to: str         # email address, or the .eml path in dry-run mode
    error: str | None


class PulseState(TypedDict, total=False):
    candidate: Candidate        # whose run this is; the graph is invoked once per candidate
    jobs: list[Job]             # new, not-yet-processed jobs found by the entry node
    results: list[JobResult]    # one per job, filled by the job agent
