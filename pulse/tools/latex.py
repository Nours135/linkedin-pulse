"""Tool: assemble a resume from component keys and compile it with pdflatex.

Inputs, per candidate in their profile folder (see pulse.candidates):
  resume.json            index of components (section, dates, directions, summary)
  components/<key>.tex   copy-paste ready LaTeX for each component and fixed section
and shared by every candidate:
  assets/resume.tex.j2   Jake's Resume preamble and section skeleton

A selection maps section -> ordered component keys, e.g.
  {"experience": ["stripe", "aws"], "projects": ["raft-kv"]}
Order is kept as given; heading, education and skills are always included.

compile_resume also reports how full the page is (a probe in the template logs the natural
height of the body), so the agent can tell "a fifth of the page left" from "spilled 3 lines".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from langchain_core.tools import tool

from pulse import candidates
from pulse.candidates import Candidate
from pulse.config import ROOT

ASSETS_DIR = ROOT / "assets"
TEMPLATE_NAME = "resume.tex.j2"

SECTIONS = {"experience": "Experience", "projects": "Projects"}  # render order -> heading
PDFLATEX_TIMEOUT = 60

# Layout levels, loosest first. Level 0 is stock Jake's Resume; each later level squeezes more
# onto the page: margin is the final top/bottom margin (in), item_gap the space after a bullet
# (pt), font the base size (pt). Used to absorb a small overflow instead of dropping content.
LAYOUTS = [
    {"margin": 0.5, "item_gap": -2, "font": 11},
    {"margin": 0.4, "item_gap": -2, "font": 11},
    {"margin": 0.35, "item_gap": -3, "font": 11},
    {"margin": 0.3, "item_gap": -4, "font": 11},
]

_env = Environment(
    loader=FileSystemLoader(ASSETS_DIR),
    variable_start_string="((", variable_end_string="))",
    block_start_string="(%", block_end_string="%)",
    comment_start_string="(#", comment_end_string="#)",
    trim_blocks=True, lstrip_blocks=True,
    undefined=StrictUndefined,
    autoescape=False,
)


def load_index(cand: Candidate) -> dict:
    return json.loads(cand.index_path.read_text())


def _component_tex(cand: Candidate, key: str) -> str:
    path = cand.components_dir / f"{key}.tex"
    if not path.exists():
        raise ValueError(f"missing component file: {path.relative_to(ROOT)}")
    return path.read_text().rstrip()


# ---- Plain-text views of the resume for LLM prompts ----

def _unescape(tex: str) -> str:
    return re.sub(r"\\([%&$#_])", r"\1", tex).replace("--", "-")


def _items(tex: str) -> list[str]:
    return [_unescape(i) for i in re.findall(r"\\resumeItem\{(.*)\}\s*$", tex, flags=re.MULTILINE)]


def bullets(cand: Candidate, key: str) -> list[str]:
    """Bullet text of a component, with LaTeX escapes undone for readability."""
    return _items(_component_tex(cand, key))


def format_education(cand: Candidate) -> str:
    """One line per school (school, location, degree, dates) followed by its bullets."""
    lines = []
    # each \resumeSubheading{school}{location}{degree}{dates} starts one school entry
    for entry in _component_tex(cand, "education").split(r"\resumeSubheading")[1:]:
        m = re.match(r"\s*\{(.*?)\}\{(.*?)\}\s*\{(.*?)\}\{(.*?)\}", entry)
        if not m:
            continue
        lines.append("- " + " | ".join(_unescape(g) for g in m.groups()))
        lines.extend(f"    * {b}" for b in _items(entry))
    return "\n".join(lines)


def format_catalog(cand: Candidate, index: dict | None = None) -> str:
    """Every component with key, dates, directions and its full bullets, grouped by section."""
    index = index or load_index(cand)
    lines = []
    for section, title in SECTIONS.items():
        lines.append(f"[{title}]")
        for key, c in index["components"].items():
            if c["section"] != section:
                continue
            lines.append(f"- {key} | {c['title']} | {c['start']} to {c['end']} | "
                         f"directions: {', '.join(c['directions'])}")
            lines.extend(f"    * {b}" for b in bullets(cand, key))
    return "\n".join(lines)


def resume_text(cand: Candidate, index: dict | None = None) -> str:
    """The whole candidate as one text block: education, component catalog and skills."""
    return (f"[Education]\n{format_education(cand)}\n\n{format_catalog(cand, index)}\n\n"
            f"[Skills]\n{_component_tex(cand, 'skills')}")


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


def render_resume(cand: Candidate, selection: dict[str, list[str]], index: dict | None = None,
                  layout: int = 0) -> str:
    """Return compile-ready LaTeX for a selection at a LAYOUTS level."""
    index = index or load_index(cand)
    validate_selection(selection, index)
    sections = [
        {"title": title, "components": [_component_tex(cand, k) for k in selection[name]]}
        for name, title in SECTIONS.items()
        if selection.get(name)
    ]
    fixed = {name: _component_tex(cand, name) for name in index["fixed"]}
    return _env.get_template(TEMPLATE_NAME).render(sections=sections, layout=LAYOUTS[layout], **fixed)


def _log_errors(log: str, context: int = 3) -> str:
    """Pull the '! ...' error lines (plus a few lines after each) out of a pdflatex log."""
    lines = log.splitlines()
    out = []
    for i, line in enumerate(lines):
        if line.startswith("!"):
            out.extend(lines[i:i + context + 1])
    return "\n".join(out) or "\n".join(lines[-20:])


def _fill(log: str, pages: int) -> float | None:
    """Pages of content as a float: 0.82 leaves 18% of the page empty, 1.07 overflows by 7%.
    Uses the natural body height logged by the template probe, so it measures the true overflow
    even when page 2 holds a whole unbreakable block. A one-page PDF is capped at 1.0 because
    TeX may shrink list glue to squeeze a natural 1.01 onto the page."""
    m = re.search(r"PULSE-FILL content=([\d.]+)pt page=([\d.]+)pt", log)
    if not m:
        return None
    fill = round(float(m.group(1)) / float(m.group(2)), 3)
    return min(fill, 1.0) if pages == 1 else fill


def compile_resume(cand: Candidate, selection: dict[str, list[str]], out_dir: Path,
                   layout: int = 0) -> dict:
    """Write out_dir/resume.tex and compile it. Never raises on bad input or LaTeX errors;
    returns {"ok", "tex", "pdf", "pages", "fill", "layout", "error"} so the caller can retry."""
    out_dir = Path(out_dir)
    result = {"ok": False, "tex": None, "pdf": None, "pages": None, "fill": None,
              "layout": layout, "error": None}
    try:
        tex = render_resume(cand, selection, layout=layout)
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
    pages = int(m.group(1))
    result.update(ok=True, pdf=str(pdf_path), pages=pages, fill=_fill(log, pages))
    return result


def pdf_to_png(pdf_path: Path, dpi: int = 100) -> list[Path]:
    """Rasterize every page with Ghostscript to <name>-<n>.png next to the PDF.
    Returns [] when gs is not installed so callers can fall back to sending the PDF itself."""
    gs = shutil.which("gs")
    if not gs:
        return []
    pdf_path = Path(pdf_path)
    for old in pdf_path.parent.glob(f"{pdf_path.stem}-*.png"):
        old.unlink()
    pattern = pdf_path.parent / f"{pdf_path.stem}-%d.png"
    subprocess.run(
        [gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=png16m", f"-r{dpi}",
         f"-sOutputFile={pattern}", str(pdf_path)],
        capture_output=True, timeout=PDFLATEX_TIMEOUT,
    )
    return sorted(pdf_path.parent.glob(f"{pdf_path.stem}-*.png"),
                  key=lambda p: int(p.stem.rsplit("-", 1)[1]))


# ---- LangChain tools for the agent ----

@tool
def build_resume(candidate: str, job_id: str, experience: list[str], projects: list[str]) -> dict:
    """Assemble and compile `candidate`'s resume for `job_id` from component keys, in the given
    order. Returns ok/pdf/pages, or an error message (unknown key, LaTeX error) to fix and retry."""
    if not re.fullmatch(r"[\w-]+", job_id):
        return {"ok": False, "error": f"invalid job_id {job_id!r}"}
    try:
        cand = candidates.get(candidate)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return compile_resume(cand, {"experience": experience, "projects": projects}, cand.work_dir / job_id)
