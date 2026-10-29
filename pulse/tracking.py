"""Your own application tracking per job: work/<candidate>/tracking.json, keyed by job id.

Kept apart from jobs.json because the pipeline rewrites jobs.json on every recorded job, while
this file is only written by you (through the web UI), so neither can clobber the other.
Each entry: status (one of STATUSES), notes, updated_at.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from pulse.candidates import Candidate

STATUSES = ("new", "applied", "oa", "interviewing", "offer", "rejected", "skipped")

_lock = threading.Lock()  # the web server is threaded; serialize read-modify-write


def _path(cand: Candidate) -> Path:
    return cand.work_dir / "tracking.json"


def load(cand: Candidate) -> dict[str, dict]:
    path = _path(cand)
    return json.loads(path.read_text()) if path.exists() else {}


def update(cand: Candidate, job_id: str, status: str | None = None, notes: str | None = None) -> dict:
    """Set status and/or notes for one job; fields left as None keep their current value."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with _lock:
        entries = load(cand)
        entry = entries.get(job_id, {"status": "new", "notes": ""})
        if status is not None:
            entry["status"] = status
        if notes is not None:
            entry["notes"] = notes
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        entries[job_id] = entry
        path = _path(cand)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2))
        tmp.replace(path)
    return entry
