"""Candidates: every person the pipeline hunts for, each with their own profile, inbox and history.

config.yaml lists them under `candidates`, keyed by a short slug:
  candidates:
    john-wang:
      profile: assets/candidates/john-wang   # resume.json + components/<key>.tex
      email_to: john.wang@example.com        # optional; falls back to EMAIL_TO, then SMTP_USER
      queries: ["Backend Engineer"]          # optional; overrides the generated search queries
      tags: {include: ["*NG"], exclude: []}  # optional; overrides match.tags key by key

Everything a candidate produces lives under work/<slug>/ (jobs.json, keywords.json, outbox/,
resumes/ and the per-job build folders), so two candidates never share seen jobs or emails.
The LLM cache and usage log stay shared in work/.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pulse.config import ROOT, settings, work_dir


@dataclass(frozen=True)
class Candidate:
    name: str                  # slug from config.yaml; used in paths, email subjects and logs
    profile_dir: Path          # holds resume.json and components/
    email_to: str = ""
    queries: tuple[str, ...] = ()
    tags_include: tuple[str, ...] = ()   # tag filter, see matching.tag_rejected
    tags_exclude: tuple[str, ...] = ()

    @property
    def index_path(self) -> Path:
        return self.profile_dir / "resume.json"

    @property
    def components_dir(self) -> Path:
        return self.profile_dir / "components"

    @property
    def work_dir(self) -> Path:
        return work_dir / self.name


def load() -> list[Candidate]:
    """All candidates from config.yaml. Raises ValueError on a bad slug or a missing profile."""
    entries = settings.get("candidates") or {}
    if not entries:
        raise ValueError("config.yaml has no candidates; add one under `candidates:`")
    default_tags = settings.get("match", {}).get("tags") or {}
    out = []
    for name, cfg in entries.items():
        cfg = cfg or {}
        if not re.fullmatch(r"[\w-]+", name):
            raise ValueError(f"candidate name {name!r} must be letters, digits, _ or -")
        profile = ROOT / cfg.get("profile", f"assets/candidates/{name}")
        if not (profile / "resume.json").is_file():
            raise ValueError(f"candidate {name}: no resume.json in {profile}")
        tags = {**default_tags, **(cfg.get("tags") or {})}
        out.append(Candidate(name=name, profile_dir=profile, email_to=cfg.get("email_to") or "",
                             queries=tuple(cfg.get("queries") or ()),
                             tags_include=tuple(tags.get("include") or ()),
                             tags_exclude=tuple(tags.get("exclude") or ())))
    return out


def get(name: str) -> Candidate:
    for c in load():
        if c.name == name:
            return c
    raise ValueError(f"unknown candidate {name!r}")
