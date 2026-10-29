"""Import a resume PDF as a new candidate profile: assets/candidates/<name>/.

    python -m pulse.importer jane.pdf --name jane-doe

1. extract   Claude reads the PDF and fills a fixed schema with plain text, copied verbatim;
             it never writes LaTeX. Sections other than work experience go to projects.
2. render    assets/component_templates/*.tex.j2 turn that text into components/<key>.tex and
             the fixed heading/education/skills, plus the resume.json index, in the same shape
             as hand-written profiles so the regex parsers in pulse.tools.latex read them back.
3. check     every bullet parses back from its .tex; every bullet occurs in the PDF's own text
             (Ghostscript txtwrite; skipped without gs); all components compile together.
4. install   only when the checks pass, the staged profile under work/<name>/import/ is copied
             to assets/candidates/<name>/. An existing profile is never overwritten.

config.yaml is not touched; the candidate entry to add is printed at the end.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pulse.candidates import Candidate
from pulse.config import ROOT, work_dir
from pulse.llm import llm, pdf
from pulse.tools import latex

TEMPLATES = "component_templates"  # under assets/, rendered with latex's Jinja environment
FIXED = ["heading", "education", "skills"]


class Basics(BaseModel):
    name: str
    email: str = Field(description="\"\" if not printed")
    phone: str = Field(description="\"\" if not printed")
    location: str = Field(description="the candidate's own location, \"\" if not printed")
    links: list[str] = Field(description="profile/website links as printed, e.g. github.com/jane")
    summary: str = Field(description="one sentence you write: who the candidate is and what roles they target")


class School(BaseModel):
    school: str
    location: str
    degree: str = Field(description="degree line as printed, including GPA if it is on that line")
    dates: str = Field(description="dates exactly as printed")
    bullets: list[str] = Field(description="lines under the school (coursework, honors), verbatim")


class Entry(BaseModel):
    section: Literal["experience", "projects"] = Field(
        description="experience for jobs, internships and research positions; projects for everything else")
    source_section: str = Field(description="the section heading it appears under in the PDF")
    name: str = Field(description="company or organization for experience; the item's name otherwise")
    role: str = Field(description="job title for experience; for projects the tech stack or role printed "
                                  "next to the name; \"\" if none")
    location: str = Field(description="\"\" if not printed")
    dates: str = Field(description="dates exactly as printed, \"\" if none")
    start: str = Field(description="YYYY-MM, \"\" if no date is printed")
    end: str = Field(description="YYYY-MM, \"present\" if ongoing, \"\" if no date is printed")
    bullets: list[str] = Field(description="every bullet under the entry, verbatim")
    directions: list[str] = Field(description="1-3 lowercase area tags")
    summary: str = Field(description="one line you write describing the entry, for an index")


class SkillGroup(BaseModel):
    category: str = Field(description="category label as printed, e.g. Languages; \"Skills\" if unlabeled")
    items: str = Field(description="the items exactly as printed, comma-separated")


class ParsedResume(BaseModel):
    basics: Basics
    education: list[School]
    entries: list[Entry] = Field(description="experience and project entries in the order printed")
    skills: list[SkillGroup]


IMPORT_PROMPT = """\
The attached PDF is a resume. Extract it into the JSON schema so it can be re-typeset.

1. Copy text verbatim: every bullet, name, title, date and skill exactly as printed, with the
   same wording, numbers and punctuation. Do not rewrite, shorten, merge, split, correct or
   translate anything. A bullet that wraps over several lines is still one bullet: join its
   lines with a single space and drop a hyphen that only marks the line break.
2. Work experience (jobs, internships, research positions) goes to section "experience".
   Every other section with entries (projects, leadership, activities, awards, publications,
   volunteering, ...) goes to "projects", with its printed heading in source_section.
   Education and skills have their own fields; a summary or objective paragraph is not an entry.
3. start/end are normalized to YYYY-MM (use month 01 when only a year is printed).
4. directions: 1-3 tags from backend, frontend, mobile, ml, data, infra, systems, security;
   add a new lowercase tag only when none of these fit.
5. basics.summary and each entry's summary are the only text you write yourself.
"""


# ---- Render ----

_TEX_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex(s: str) -> str:
    """Plain text -> LaTeX: one line, specials escaped, dashes as TeX ligatures."""
    s = " ".join(s.split())
    s = "".join(_TEX_SPECIAL.get(ch, latex.TEX_SYMBOLS.get(ch, ch)) for ch in s)
    # pdflatex prints ' and -- as curly quote and en dash, so the PDF shows the typographic forms
    for typographic, ascii_tex in [("\u2014", "---"), ("\u2013", "--"), ("\u2019", "'"), ("\u2018", "`"),
                                   ("\u201c", "``"), ("\u201d", "''")]:
        s = s.replace(typographic, ascii_tex)
    return s


def _render(template: str, **values) -> str:
    return latex._env.get_template(f"{TEMPLATES}/{template}.tex.j2").render(**values).rstrip() + "\n"


def _href(url: str) -> str:
    target = url if re.match(r"\w+:", url) else f"https://{url}"
    target = target.replace("%", r"\%").replace("#", r"\#")
    return rf"\href{{{target}}}{{\underline{{{tex(url)}}}}}"


def _heading(b: Basics) -> str:
    contacts = [tex(b.phone)] if b.phone else []
    if b.email:
        contacts.append(rf"\href{{mailto:{b.email}}}{{\underline{{{tex(b.email)}}}}}")
    contacts += [_href(link) for link in b.links]
    return _render("heading", name=tex(b.name), contacts=contacts)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "item"


def _keys(entries: list[Entry]) -> list[str]:
    """One unique file-safe key per entry, from its name; fixed section names are reserved."""
    taken, keys = set(FIXED), []
    for e in entries:
        base = key = _slug(e.name)
        n = 2
        while key in taken:
            key, n = f"{base}-{n}", n + 1
        taken.add(key)
        keys.append(key)
    return keys


def _clean(parsed: ParsedResume) -> ParsedResume:
    """Drop empty bullets and entries the model returned without a name."""
    for group in [parsed.education, parsed.entries]:
        for item in group:
            item.bullets = [" ".join(b.split()) for b in item.bullets if b.strip()]
    parsed.entries = [e for e in parsed.entries if e.name.strip()]
    return parsed


def write_profile(parsed: ParsedResume, profile_dir: Path,
                  append: bool = False) -> tuple[dict, dict[str, list[str]]]:
    """Write resume.json and components/ into profile_dir. Returns the index and the extracted
    bullets per key, which the checks compare the written .tex against.
    append is the hook for merging into an existing profile (dedupe by key and dates)."""
    if append:
        raise NotImplementedError("merging into an existing profile is not implemented yet")
    components = profile_dir / "components"
    components.mkdir(parents=True)
    (components / "heading.tex").write_text(_heading(parsed.basics))
    (components / "education.tex").write_text(_render("education", schools=[
        {"school": tex(s.school), "location": tex(s.location), "degree": tex(s.degree),
         "dates": tex(s.dates), "bullets": [tex(b) for b in s.bullets]} for s in parsed.education]))
    (components / "skills.tex").write_text(_render("skills", groups=[
        {"category": tex(g.category), "skills": tex(g.items)} for g in parsed.skills]))

    index = {"basics": parsed.basics.model_dump(), "fixed": FIXED, "components": {}}
    extracted = {}
    for key, e in zip(_keys(parsed.entries), parsed.entries):
        extracted[key] = e.bullets
        template = "experience" if e.section == "experience" else "project"
        (components / f"{key}.tex").write_text(_render(
            template, name=tex(e.name), role=tex(e.role), location=tex(e.location),
            dates=tex(e.dates), bullets=[tex(b) for b in e.bullets]))
        if e.section == "experience":
            title = f"{e.name} - {e.role}" if e.role else e.name
        else:
            title = f"{e.name} ({e.role})" if e.role else e.name
        index["components"][key] = {
            "section": e.section, "title": title, "start": e.start, "end": e.end,
            "directions": [d.lower() for d in e.directions], "summary": e.summary,
            **({"source_section": e.source_section} if e.section == "projects" else {}),
        }
    (profile_dir / "resume.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    return index, extracted


# ---- Check ----

def _norm(s: str) -> str:
    """Compare text ignoring case, spacing and punctuation. PDF text layers often decode the
    ff/fi/fl/ffi/ffl ligatures as stray glyphs (e.g. stuffing -> stuf?ng), so non-ASCII is dropped
    and f, i, l are removed on both sides."""
    s = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", s).lower())
    return re.sub(r"[fil]", "", s)


def pdf_text(pdf_path: Path) -> str | None:
    """The PDF's text layer via Ghostscript, or None when gs is missing."""
    gs = shutil.which("gs")
    if not gs:
        return None
    out = subprocess.run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=txtwrite",
                          "-sOutputFile=-", str(pdf_path)],
                         capture_output=True, text=True, timeout=latex.PDFLATEX_TIMEOUT)
    return out.stdout


def check(cand: Candidate, index: dict, extracted: dict[str, list[str]], source_pdf: Path,
          preview_dir: Path) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors block the install; warnings are for the user to review."""
    errors, warnings = [], []
    components = index["components"]

    # the .tex must parse back into exactly the extracted bullets, or the LLM prompts lose them
    for key, bullets in extracted.items():
        expected = [latex._unescape(tex(b)) for b in bullets]
        if latex.bullets(cand, key) != expected:
            errors.append(f"{key}: bullets do not parse back from components/{key}.tex")
    if not latex.format_education(cand):
        errors.append("education.tex: no school parses back")

    # verbatim: every bullet must occur in the source PDF's own text
    text = pdf_text(source_pdf)
    if text is None:
        warnings.append("gs not found; skipped the verbatim check against the PDF text")
    elif not _norm(text):
        warnings.append("the PDF has no text layer (scanned?); skipped the verbatim check")
    else:
        source = _norm(text)
        for key, bullets in extracted.items():
            for b in bullets:
                if _norm(b) not in source:
                    warnings.append(f"{key}: bullet not found verbatim in the PDF: {b}")

    # everything together must compile; the preview may run past one page
    selection = {s: [k for k, c in components.items() if c["section"] == s] for s in latex.SECTIONS}
    result = latex.compile_resume(cand, selection, preview_dir)
    if result["ok"]:
        print(f"[import] preview of all components ({result['pages']} pages): {result['pdf']}")
    else:
        errors.append(f"compile failed: {result['error']}")
    return errors, warnings


# ---- Entry point ----

def import_pdf(source_pdf: Path, name: str, model: str | None = None) -> Path:
    """Extract, render, check and install. Returns the installed profile directory."""
    if not re.fullmatch(r"[\w-]+", name):
        raise SystemExit(f"candidate name {name!r} must be letters, digits, _ or -")
    if not source_pdf.is_file():
        raise SystemExit(f"no such file: {source_pdf}")
    target = ROOT / "assets" / "candidates" / name
    if target.exists():  # checked before the LLM call so a mistake costs nothing
        raise SystemExit(f"{target.relative_to(ROOT)} already exists; pick another --name or remove it")

    staging = work_dir / name / "import"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    shutil.copy2(source_pdf, staging / "source.pdf")

    print(f"[import] extracting {source_pdf.name} ...")
    parsed = _clean(llm.parse([pdf(source_pdf), IMPORT_PROMPT], ParsedResume,
                              model=model, tag="import.extract"))
    (staging / "parsed.json").write_text(parsed.model_dump_json(indent=2))
    profile = staging / "profile"
    index, extracted = write_profile(parsed, profile)
    n = {s: sum(c["section"] == s for c in index["components"].values()) for s in latex.SECTIONS}
    print(f"[import] {n['experience']} experience, {n['projects']} projects, "
          f"{len(parsed.education)} schools, {len(parsed.skills)} skill groups")

    errors, warnings = check(Candidate(name=name, profile_dir=profile), index, extracted,
                             source_pdf, staging / "preview")
    for w in warnings:
        print(f"[import] WARNING {w}")
    if errors:
        for e in errors:
            print(f"[import] ERROR {e}")
        raise SystemExit(f"import failed; nothing installed. Staged files: {staging.relative_to(ROOT)}")

    shutil.copytree(profile, target)
    print(f"[import] installed {target.relative_to(ROOT)}\n")
    print("Add to config.yaml under `candidates:` to enable it:")
    print(f"  {name}:\n    profile: {target.relative_to(ROOT)}")
    if parsed.basics.email:
        print(f"    email_to: {parsed.basics.email}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a resume PDF as a new candidate profile.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--name", required=True, help="candidate slug, e.g. jane-doe")
    parser.add_argument("--model", help="Claude model for extraction (default: llm.model in config.yaml)")
    args = parser.parse_args()
    import_pdf(args.pdf, args.name, args.model)


if __name__ == "__main__":
    main()
