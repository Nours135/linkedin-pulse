"""Job polling agent: keyword pre-filter -> match scoring -> dispatch resume agent on demand -> email.
Fixed pipeline; the LLM is only used for keyword generation and match scoring."""

from __future__ import annotations

from pulse import store
from pulse.agents import resume_agent
from pulse.config import settings
from pulse.state import JobResult, PulseState
from pulse.tools import email, matching
from pulse.tools.linkedin import Job, fetch_job_detail


def _done(job: Job, status: str, **info) -> JobResult:
    store.mark(job.id, status, title=job.title, company=job.company, **info)
    return JobResult(job_id=job.id, title=job.title, company=job.company, url=job.url, status=status, **info)


def _build_resume(job: Job) -> dict:
    # a failed resume should not block the email; it goes out without the attachment
    try:
        return resume_agent.run_resume_agent(job)
    except Exception as e:
        return {"ok": False, "pdf": None, "error": f"{type(e).__name__}: {e}"}


def process_job(job: Job, kw: matching.Keywords) -> JobResult:
    cfg = settings["match"]
    if matching.title_excluded(job.title, kw):
        return _done(job, "filtered", reason="title")

    fetch_job_detail(job)
    hits = matching.keyword_hits(job.description, kw)
    if len(hits) < cfg["min_keyword_hits"]:
        return _done(job, "filtered", reason=f"{len(hits)} keyword hits")

    match = matching.score_match(job)
    if match.score < cfg["min_score"]:
        return _done(job, "low_score", score=match.score)

    resume = _build_resume(job)
    sent_to = email.send(email.compose(job, match, resume), job.id)
    return _done(job, "sent", score=match.score, pdf=resume.get("pdf"),
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
