"""Tool: assemble a resume from component keys and compile it with pdflatex.

Inputs live in assets/:
  resume.json            index of components (section, dates, directions, summary)
  components/<key>.tex   copy-paste ready LaTeX for each component and fixed section
  resume.tex.j2          Jake's Resume preamble and section skeleton

A selection maps section -> ordered component keys, e.g.
  {"experience": ["stripe", "aws"], "projects": ["raft-kv"]}
Order is kept as given; heading, education and skills are always included.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from langchain_core.tools import tool

ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = ROOT / "assets"
COMPONENTS_DIR = ASSETS_DIR / "components"
INDEX_PATH = ASSETS_DIR / "resume.json"
TEMPLATE_NAME = "resume.tex.j2"
WORK_DIR = ROOT / "work"

SECTIONS = {"experience": "Experience", "projects": "Projects"}  # render order -> heading
PDFLATEX_TIMEOUT = 60

_env = Environment(
    loader=FileSystemLoader(ASSETS_DIR),
    variable_start_string="((", variable_end_string="))",
    block_start_string="(%", block_end_string="%)",
    comment_start_string="(#", comment_end_string="#)",
    trim_blocks=True, lstrip_blocks=True,
    undefined=StrictUndefined,
    autoescape=False,
)


def load_index(path: Path = INDEX_PATH) -> dict:
    return json.loads(path.read_text())


def _component_tex(key: str) -> str:
    path = COMPONENTS_DIR / f"{key}.tex"
    if not path.exists():
        raise ValueError(f"missing component file: {path.relative_to(ROOT)}")
    return path.read_text().rstrip()


def validate_selection(selection: dict[str, list[str]], index: dict) -> None:
    """Raise ValueError on unknown sections, unknown keys, wrong section or duplicates."""
    components = index["components"]
    errors = []
    for section, keys in selection.items():
        if section not in SECTIONS:
            errors.append(f"unknown section {section!r}; expected one of {list(SECTIONS)}")
            continue
        seen = set()
        for key in keys:
            if key in seen:
                errors.append(f"duplicate key {key!r} in {section}")
            seen.add(key)
            if key not in components:
                errors.append(f"unknown key {key!r}")
            elif components[key]["section"] != section:
                errors.append(f"key {key!r} belongs to {components[key]['section']}, not {section}")
    if errors:
        raise ValueError("; ".join(errors))


def render_resume(selection: dict[str, list[str]], index: dict | None = None) -> str:
    """Return compile-ready LaTeX for a selection."""
    index = index or load_index()
    validate_selection(selection, index)
    sections = [
        {"title": title, "components": [_component_tex(k) for k in selection[name]]}
        for name, title in SECTIONS.items()
        if selection.get(name)
    ]
    fixed = {name: _component_tex(name) for name in index["fixed"]}
    return _env.get_template(TEMPLATE_NAME).render(sections=sections, **fixed)


def _log_errors(log: str, context: int = 3) -> str:
    """Pull the '! ...' error lines (plus a few lines after each) out of a pdflatex log."""
    lines = log.splitlines()
    out = []
    for i, line in enumerate(lines):
        if line.startswith("!"):
            out.extend(lines[i:i + context + 1])
    return "\n".join(out) or "\n".join(lines[-20:])


def compile_resume(selection: dict[str, list[str]], out_dir: Path) -> dict:
    """Write out_dir/resume.tex and compile it. Never raises on bad input or LaTeX errors;
    returns {"ok", "tex", "pdf", "pages", "error"} so the caller can retry."""
    out_dir = Path(out_dir)
    result = {"ok": False, "tex": None, "pdf": None, "pages": None, "error": None}
    try:
        tex = render_resume(selection)
    except ValueError as e:
        result["error"] = str(e)
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / "resume.tex"
    tex_path.write_text(tex)
    result["tex"] = str(tex_path)

    try:
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
            cwd=out_dir, capture_output=True, timeout=PDFLATEX_TIMEOUT,
        )
    except FileNotFoundError:
        result["error"] = "pdflatex not found on PATH"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = f"pdflatex timed out after {PDFLATEX_TIMEOUT}s"
        return result

    log_path = out_dir / "resume.log"
    log = log_path.read_text(errors="replace") if log_path.exists() else ""
    pdf_path = out_dir / "resume.pdf"
    m = re.search(r"Output written on .*?\((\d+) pages?", log)
    if not m or not pdf_path.exists():
        result["error"] = _log_errors(log) or "pdflatex produced no output"
        return result

    for ext in (".aux", ".out"):
        (out_dir / f"resume{ext}").unlink(missing_ok=True)
    result.update(ok=True, pdf=str(pdf_path), pages=int(m.group(1)))
    return result


# ---- LangChain tools for the agent ----

@tool
def build_resume(job_id: str, experience: list[str], projects: list[str]) -> dict:
    """Assemble and compile a resume for `job_id` from component keys, in the given order.
    Returns ok/pdf/pages, or an error message (unknown key, LaTeX error) to fix and retry."""
    if not re.fullmatch(r"[\w-]+", job_id):
        return {"ok": False, "error": f"invalid job_id {job_id!r}"}
    return compile_resume({"experience": experience, "projects": projects}, WORK_DIR / job_id)
