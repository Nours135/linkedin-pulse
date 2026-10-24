"""Load config.yaml / .env and expose ROOT, settings and work_dir."""

from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Secrets (ANTHROPIC_API_KEY, SMTP_*) come from .env; real env vars take precedence
load_dotenv(ROOT / ".env")

settings: dict = yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
work_dir = ROOT / settings.get("work_dir", "work")
