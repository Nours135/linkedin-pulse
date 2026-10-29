"""Resume agent: a sub-agent of the job agent that tailors a one-page resume to one JD.

Step 1 (select): the LLM picks experience and project keys for the JD and match result.
Step 2 (fit): a fixed loop compiles the selection and measures how full the page is
  - fill < MIN_FILL           the LLM adds components, trading relevance against prestige
  - small overflow            try tighter LAYOUTS levels (margins, bullet gaps), no LLM call
  - large or unsqueezable     the LLM drops components
until the page is full enough, the LLM declines to change it, or MAX_ROUNDS is hit. Every LLM
call sees the full component catalog (all bullets) and an image of the latest compiled version.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from langchain_core.tools import tool
from tqdm import tqdm

from pulse import candidates
from pulse.candidates import Candidate
from pulse.llm import LLMError, image, llm, pdf
from pulse.tools.latex import (
    LAYOUTS, SECTIONS, compile_resume, format_catalog, load_index, pdf_to_png, validate_selection,
)

# Calibrated with pdflatex on the Jake's Resume template: experiences carry 3 one-line bullets,
# projects 2, so 5+3 or 4+4 fills exactly one page and a 9th component spills to page 2.
MAX_COMPONENTS = 8
MIN_FILL = 0.9         # below this share of the page, ask the LLM for more content
SQUEEZE_LIMIT = 0.2    # overflow (in pages) small enough to try tighter layouts first
MAX_ROUNDS = 6         # fit-loop compiles that may involve an LLM decision
EST_HEIGHT = {"experience": 0.10, "projects": 0.07}  # page share of one component at layout 0

SYSTEM_PROMPT = """\
You tailor a one-page resume to a job description by choosing components from a catalog.
Each component is a pre-written block (an experience or a project) identified by its key; you
cannot edit bullets, only choose which blocks appear and in what order. Heading, education and
skills are always included.

Rules:
1. One page. At the default layout a page holds about {max_components} components, e.g.
   5 experiences + 3 projects or 4 + 4. One experience takes about {exp_h:.0%} of the page,
   one project about {prj_h:.0%}.
2. Relevance first: prefer components whose directions and bullets match the JD and the match
   result. When the JD's own direction runs out, fill with the closest adjacent directions.
3. Weigh relevance against strength: a well-known company, a full-time-scale internship,
   measurable impact or a publication can beat a slightly more relevant but weaker item.
4. Order: experience keys in reverse chronological order by end date ("present" first, ties
   broken by later start). Project keys by relevance to the JD, most relevant first.
5. Use only keys from the catalog, each at most once, in the section it belongs to.

Always reply with JSON only:
{{"experience": ["<key>", ...], "projects": ["<key>", ...], "reason": "<one sentence>"}}

Catalog:
{catalog}
"""

SELECT_TASK = """\
Step 1: choose the components for this job.

Job description:
{jd}

Match result from the job agent:
{match}
"""

ADD_TASK = """\
Step 2: the compiled resume (image attached) fills only {fill:.0%} of the page, leaving about
{left:.0%} empty. Add components that are worth the space, judged by relevance and strength,
and keep the ordering rules. Overflowing slightly is fine: small overflows are fixed by
tightening the layout. If nothing left in the catalog is worth adding, return the current
selection unchanged and say why in "reason".
"""

REMOVE_TASK = """\
Step 2: the compiled resume (image attached) runs to {pages} pages: the content is {fill:.0%} of
one page, so about {over:.0%} of a page must go, more than tightening the layout can absorb.
Remove the components that are least worth their space, judged by relevance and strength.
Keep the ordering rules.
"""

CONTEXT = """\
Job description:
{jd}

Match result:
{match}

Current selection: {selection}
Selections already tried (do not return one of these): {tried}
"""


def system_prompt(cand: Candidate, index: dict | None = None) -> str:
    return SYSTEM_PROMPT.format(
        max_components=MAX_COMPONENTS, exp_h=EST_HEIGHT["experience"],
        prj_h=EST_HEIGHT["projects"], catalog=format_catalog(cand, index),
    )


def _preview(result: dict) -> list[dict]:
    """Image blocks of the latest compiled PDF (PDF block if Ghostscript is missing)."""
    pngs = pdf_to_png(Path(result["pdf"]))
    return [image(p) for p in pngs] if pngs else [pdf(result["pdf"])]


def _ask(content: list, system: str, index: dict, tag: str) -> dict[str, list[str]]:
    """One LLM decision, parsed and validated; a bad reply is sent back once to be fixed."""
    error = None
    for _ in range(2):
        prompt = content + ([f"Your previous reply was invalid: {error}. Reply again."] if error else [])
        try:
            reply = llm.complete_json(prompt, system=system, tag=tag)
            selection = {name: list(reply.get(name, [])) for name in SECTIONS}
            validate_selection(selection, index)
            return selection
        except (ValueError, AttributeError) as e:  # JSONDecodeError is a ValueError
            error = str(e)
    raise LLMError(f"{tag}: no valid selection after retry: {error}")


def _fit_layout(cand: Candidate, selection: dict, out_dir: Path) -> dict:
    """Compile at layout 0; if it overflows by at most SQUEEZE_LIMIT, try tighter layouts.
    Returns the first one-page result, or the layout-0 result when squeezing can't help."""
    first = compile_resume(cand, selection, out_dir)
    if not first["ok"] or first["pages"] == 1 or first["fill"] - 1 > SQUEEZE_LIMIT:
        return first
    for level in range(1, len(LAYOUTS)):
        result = compile_resume(cand, selection, out_dir, layout=level)
        if result["ok"] and result["pages"] == 1:
            return result
    return compile_resume(cand, selection, out_dir)  # leave the unsqueezed version on disk


def tailor(cand: Candidate, job_id: str, jd: str, match: dict | str, out_dir: Path | None = None) -> dict:
    """Run select + fit of `cand`'s resume for one job. Returns the final compile result plus the
    selection and a trace of the fit loop; ok is False only if nothing compiled to a single page."""
    index = load_index(cand)
    out_dir = Path(out_dir or cand.work_dir / job_id)
    system = system_prompt(cand, index)
    match = match if isinstance(match, str) else json.dumps(match, indent=2)

    # one step for the select call, then one per fit round; nested under the job agent's bar
    with tqdm(total=MAX_ROUNDS + 1, desc=f"  resume {cand.name}/{job_id}", unit="step", leave=False) as bar:
        bar.set_postfix_str("selecting components")
        selection = _ask([SELECT_TASK.format(jd=jd, match=match)], system, index, "resume.select")
        bar.update()
        tried = [selection]
        trace = []
        best = None  # latest selection that compiled to one page

        for _ in range(MAX_ROUNDS):
            bar.set_postfix_str("compiling")
            result = _fit_layout(cand, selection, out_dir)
            if not result["ok"]:
                raise LLMError(f"compile failed for {selection}: {result['error']}")
            trace.append({"selection": selection, "pages": result["pages"],
                          "fill": result["fill"], "layout": result["layout"]})
            status = f"{result['pages']}p fill {result['fill']:.0%} L{result['layout']}"
            if result["pages"] == 1:
                best = (selection, result)
                if result["fill"] >= MIN_FILL:
                    bar.update()
                    break
                task = ADD_TASK.format(fill=result["fill"], left=1 - result["fill"])
                bar.set_postfix_str(f"{status}, asking LLM to add")
            else:
                task = REMOVE_TASK.format(pages=result["pages"], fill=result["fill"],
                                          over=result["fill"] - 1)
                bar.set_postfix_str(f"{status}, asking LLM to remove")

            context = CONTEXT.format(jd=jd, match=match, selection=json.dumps(selection),
                                     tried=json.dumps(tried[:-1]))
            proposal = _ask([task, context, *_preview(result)], system, index, "resume.fit")
            bar.update()
            if proposal == selection or proposal in tried:
                trace.append({"stop": "no new selection proposed"})
                break
            selection = proposal
            tried.append(selection)

    if best is None:
        return {**result, "ok": False, "selection": selection, "trace": trace,
                "error": "no selection fit on one page"}
    final_selection, final = best
    if result is not final:  # a later attempt overwrote the PDF; rebuild the chosen one
        final = _fit_layout(cand, final_selection, out_dir)
    return {**final, "selection": final_selection, "trace": trace}


# ---- LangChain tool for the job agent ----

@tool
def tailor_resume(candidate: str, job_id: str, jd: str, match: dict) -> dict:
    """Build `candidate`'s one-page resume tailored to a job: pick their most relevant
    experiences and projects for the JD and match result, then fit them to exactly one page.
    Returns the PDF path, page fill, chosen keys and a trace of the fit loop."""
    if not re.fullmatch(r"[\w-]+", job_id):
        return {"ok": False, "error": f"invalid job_id {job_id!r}"}
    try:
        cand = candidates.get(candidate)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return tailor(cand, job_id, jd, match)
