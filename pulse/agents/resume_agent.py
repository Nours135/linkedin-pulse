"""Resume ensemble agent: pick resume components for a JD and assemble a LaTeX resume in the work folder.
Fixed pipeline; no coding-agent freedom."""

from __future__ import annotations

from pulse.tools.latex import SECTIONS, load_index

# Calibrated with pdflatex on the Jake's Resume template: experiences carry 3 one-line bullets,
# projects 2, so 5+3 or 4+4 fills exactly one page and a 9th component spills to page 2.
MAX_COMPONENTS = 8

SELECTION_PROMPT = """\
You are tailoring a one-page resume to a job description by choosing components from a catalog.
Each component is a pre-written block (an experience or a project) identified by its key.
Heading, education and skills are always included; you only choose experience and projects.

Rules:
1. One page: choose at most {max_components} components in total, e.g. 5 experiences + 3 projects
   or 4 experiences + 4 projects. Prefer the split whose extra item is more relevant to the JD.
2. Relevance first: pick components whose directions and summary match the JD. When the JD's own
   direction runs out, fill the remaining slots with the closest adjacent directions rather than
   leaving the page short.
3. Order: list experience keys in reverse chronological order by end date ("present" first, ties
   broken by later start). List project keys by relevance to the JD, most relevant first.
4. Use only keys from the catalog, each at most once, in the section it belongs to.

Reply with JSON only:
{{"experience": ["<key>", ...], "projects": ["<key>", ...]}}

Catalog:
{catalog}
"""


def format_catalog(index: dict | None = None) -> str:
    """One line per component: key, dates, directions and summary, grouped by section."""
    index = index or load_index()
    lines = []
    for section, title in SECTIONS.items():
        lines.append(f"[{title}]")
        for key, c in index["components"].items():
            if c["section"] != section:
                continue
            lines.append(
                f"- {key} | {c['title']} | {c['start']} to {c['end']} | "
                f"directions: {', '.join(c['directions'])} | {c['summary']}"
            )
    return "\n".join(lines)


def selection_prompt(index: dict | None = None) -> str:
    return SELECTION_PROMPT.format(max_components=MAX_COMPONENTS, catalog=format_catalog(index))
