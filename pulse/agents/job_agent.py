"""Job polling agent: age/title filter -> keyword pre-filter -> match scoring -> resume agent -> email.
Fixed pipeline; the LLM is only used for keyword generation and match scoring.
Every outcome is recorded in pulse.store."""

from __future__ import annotations

from datetime import date, timedelta

from pulse import store
from pulse.agents import resume_agent
from pulse.config import settings
from pulse.state import JobResult, PulseState
from pulse.tools import email, matching
from pulse.tools.linkedin import Job, fetch_job_detail


def _done(job: Job, status: str, **info) -> JobResult:
    store.record(job, status, **info)
    shown = {k: info[k] for k in ("reason", "score", "sent_to", "error") if k in info}
    if "resume_pdf" in info:
        shown["pdf"] = info["resume_pdf"]
    return JobResult(job_id=job.id, title=job.title, company=job.company, url=job.url,
                     status=status, **shown)


def too_old(job: Job, max_age_days: int) -> bool:
    """True if posted more than max_age_days ago; an unparsable date is kept."""
    try:
        posted = date.fromisoformat(job.posted_at)
    except ValueError:
        return False
    return posted < date.today() - timedelta(days=max_age_days)


def _build_resume(job: Job, match: matching.Match) -> dict:
    # a failed resume should not block the email; it goes out without the attachment
    try:
        resume = resume_agent.tailor(job.id, job.description, match.model_dump())
    except Exception as e:
        return {"ok": False, "pdf": None, "error": f"{type(e).__name__}: {e}"}
    if resume.get("pdf"):
        resume["pdf"] = store.persist_resume(job.id, resume["pdf"])
    return resume


def process_job(job: Job, kw: matching.Keywords) -> JobResult:
    cfg = settings["match"]
    max_age = settings["filter"]["max_age_days"]
    if too_old(job, max_age):
        return _done(job, "filtered", reason=f"posted {job.posted_at}, older than {max_age} days")
    if matching.title_excluded(job.title, kw):
        return _done(job, "filtered", reason="title")

    fetch_job_detail(job)
    hits = matching.keyword_hits(job.description, kw)
    if len(hits) < cfg["min_keyword_hits"]:
        return _done(job, "filtered", reason=f"{len(hits)} keyword hits", hits=hits)

    match = matching.score_match(job)
    if match.score < cfg["min_score"]:
        return _done(job, "low_score", hits=hits, score=match.score, match=match.model_dump())

    resume = _build_resume(job, match)
    sent_to = email.send(email.compose(job, match, resume), job.id)
    return _done(job, "sent", hits=hits, score=match.score, match=match.model_dump(),
                 resume_pdf=resume.get("pdf"),
                 error=resume.get("error"), sent_to=sent_to)


def run(state: PulseState) -> dict:
    kw = matching.generate_keywords()
    results = []
    for job in state["jobs"]:
        try:
            results.append(process_job(job, kw))
        except Exception as e:
            # not recorded in the store, so the job is retried next heartbeat
            results.append(JobResult(job_id=job.id, title=job.title, company=job.company, url=job.url,
                                     status="error", error=f"{type(e).__name__}: {e}"))
    return {"results": results}
