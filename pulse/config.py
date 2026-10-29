"""Load config.yaml / .env and expose ROOT, settings and work_dir.
config.yaml is the local, gitignored config; config.example.yaml is used when it is missing."""

from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Secrets (ANTHROPIC_API_KEY, SMTP_*) come from .env; real env vars take precedence
load_dotenv(ROOT / ".env")

CONFIG_PATH = ROOT / "config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = ROOT / "config.example.yaml"
    print("[config] config.yaml not found; using config.example.yaml (cp it to config.yaml to customize)")
settings: dict = yaml.safe_load(CONFIG_PATH.read_text()) or {}
work_dir = ROOT / settings.get("work_dir", "work")
