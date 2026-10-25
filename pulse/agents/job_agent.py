"""Job polling agent: age/title filter -> keyword pre-filter -> match scoring -> resume agent -> email.
Fixed pipeline; the LLM is only used for keyword generation and match scoring.
Every outcome is recorded in pulse.store."""

from __future__ import annotations

from datetime import date, timedelta

from tqdm import tqdm

from pulse import store
from pulse.agents import resume_agent
from pulse.config import settings
from pulse.state import JobResult, PulseState
from pulse.tools import email, matching
from pulse.tools.linkedin import Job, fetch_job_detail

# resume-agent fields kept in jobs.json; the full fit-loop trace goes next to the archived PDF
RESUME_SUMMARY = ("selection", "pages", "fill", "layout")


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
        tqdm.write(f"  resume {job.id} failed: {type(e).__name__}: {e}")
        return {"ok": False, "pdf": None, "error": f"{type(e).__name__}: {e}"}
    if resume.get("pdf"):
        meta = {k: resume.get(k) for k in ("ok", "error", *RESUME_SUMMARY, "trace")}
        resume["pdf"] = store.persist_resume(job.id, resume["pdf"], meta)
    fill = f"{resume['fill']:.0%}" if resume.get("fill") is not None else "?"
    tqdm.write(f"  resume {job.id}: {'ok' if resume.get('ok') else 'FAILED'}, {resume.get('pages')} page(s), "
               f"fill {fill}, layout {resume.get('layout')}, {len(resume.get('trace', []))} trace steps"
               + (f" ({resume['error']})" if resume.get("error") else ""))
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
                 resume={k: resume.get(k) for k in RESUME_SUMMARY},
                 error=resume.get("error"), sent_to=sent_to)


def run(state: PulseState) -> dict:
    kw = matching.generate_keywords()
    results = []
    bar = tqdm(state["jobs"], desc="jobs", unit="job")
    for job in bar:
        bar.set_postfix_str(f"{job.title[:30]} @ {job.company[:20]}")
        try:
            results.append(process_job(job, kw))
        except Exception as e:
            # recorded with an attempts count; retried next heartbeat until retry.max_attempts
            results.append(_done(job, "error", error=f"{type(e).__name__}: {e}"))
    return {"results": results}
