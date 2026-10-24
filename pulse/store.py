"""Record of every job the pipeline has seen: work/jobs.json, keyed by job id.

Each record has a stage, by how far the job got:
  found    seen in a search and filtered out before its JD was fetched (age, title):
           id, title, company, location, url, posted_at, found_at, status, reason
  fetched  JD fetched: adds a non-empty description and fetched_at, plus hits/score
  applied  email sent: adds resume_pdf (a copy under work/resumes/, kept per generation)

Jobs that errored are never recorded, so they are retried next heartbeat. Every recorded id
is skipped by the next search, whatever its stage.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from pulse.config import work_dir
from pulse.tools.linkedin import Job

PATH = work_dir / "jobs.json"
RESUMES_DIR = work_dir / "resumes"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load() -> dict[str, dict]:
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def record(job: Job, status: str, **info) -> dict:
    """Save one job's outcome. status is filtered | low_score | sent; the stage follows from it."""
    if status == "sent":
        stage = "applied"
    elif job.description:
        stage = "fetched"
    else:
        stage = "found"
    rec = {k: v for k, v in asdict(job).items() if v or k in ("posted_at", "url")}
    rec.update(stage=stage, status=status, recorded_at=_now(), **info)

    jobs = load()
    jobs[job.id] = rec
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
    tmp.replace(PATH)  # never leave a half-written store behind
    return rec


def persist_resume(job_id: str, pdf: str | Path) -> str:
    """Copy a generated resume (and its .tex) to work/resumes/<job_id>/<timestamp>.pdf.
    The resume agent's own folder is rewritten on every run, so this copy is what stays."""
    pdf = Path(pdf)
    dest = RESUMES_DIR / job_id / f"{datetime.now():%Y%m%d-%H%M%S}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, dest)
    if pdf.with_suffix(".tex").exists():
        shutil.copy2(pdf.with_suffix(".tex"), dest.with_suffix(".tex"))
    return str(dest)
