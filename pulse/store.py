"""Record of every job the pipeline has seen for a candidate: work/<candidate>/jobs.json, keyed
by job id. Each candidate has their own record, so a job one candidate skipped is still new to
another.

Each record has a stage, by how far the job got:
  found    seen in a search and filtered out before its JD was fetched (age, title):
           id, title, company, location, url, posted_at, found_at, status, reason
  fetched  JD fetched: adds a non-empty description and fetched_at, plus hits/score
  applied  email sent: adds resume_pdf (a copy under work/<candidate>/resumes/, kept per generation) and
           resume (selection, pages, fill, layout); the fit-loop trace sits next to the PDF
           as <timestamp>.json

A job that raised is recorded with status "error", its latest error and an attempts count,
at stage found or fetched depending on how far it got. Every recorded id is skipped by the
next search, except errors with fewer than retry.max_attempts attempts, which are retried.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from pulse.candidates import Candidate
from pulse.config import settings
from pulse.tools.linkedin import Job

MAX_ATTEMPTS = settings.get("retry", {}).get("max_attempts", 3)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path(cand: Candidate) -> Path:
    return cand.work_dir / "jobs.json"


def load(cand: Candidate) -> dict[str, dict]:
    path = _path(cand)
    return json.loads(path.read_text()) if path.exists() else {}


def skip(rec: dict | None) -> bool:
    """True if a job with this record should not be processed again."""
    if rec is None:
        return False
    return rec["status"] != "error" or rec.get("attempts", 1) >= MAX_ATTEMPTS


def record(cand: Candidate, job: Job, status: str, **info) -> dict:
    """Save one job's outcome. status is filtered | low_score | sent | error; the stage follows from it."""
    if status == "sent":
        stage = "applied"
    elif job.description:
        stage = "fetched"
    else:
        stage = "found"
    jobs = load(cand)
    if status == "error":
        info["attempts"] = jobs.get(job.id, {}).get("attempts", 0) + 1
    rec = {k: v for k, v in asdict(job).items() if v or k in ("posted_at", "url")}
    rec.update(stage=stage, status=status, recorded_at=_now(), **info)

    jobs[job.id] = rec
    path = _path(cand)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
    tmp.replace(path)  # never leave a half-written store behind
    return rec


def persist_resume(cand: Candidate, job_id: str, pdf: str | Path, meta: dict | None = None) -> str:
    """Copy a generated resume (and its .tex) to work/<candidate>/resumes/<job_id>/<timestamp>.pdf, and
    write meta (selection, fit-loop trace) beside it as <timestamp>.json.
    The resume agent's own folder is rewritten on every run, so this copy is what stays."""
    pdf = Path(pdf)
    dest = cand.work_dir / "resumes" / job_id / f"{datetime.now():%Y%m%d-%H%M%S}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, dest)
    if pdf.with_suffix(".tex").exists():
        shutil.copy2(pdf.with_suffix(".tex"), dest.with_suffix(".tex"))
    if meta is not None:
        dest.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return str(dest)
