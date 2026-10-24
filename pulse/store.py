"""Dedup record of already-processed jobs: work/seen_jobs.json."""

from __future__ import annotations

import json
from datetime import datetime

from pulse.config import work_dir

PATH = work_dir / "seen_jobs.json"


def load() -> dict[str, dict]:
    """job_id -> {"status", "at", ...}. Jobs that errored are never recorded, so they get retried."""
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def mark(job_id: str, status: str, **info) -> None:
    seen = load()
    seen[job_id] = {"status": status, "at": datetime.now().isoformat(timespec="seconds"), **info}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=2))
