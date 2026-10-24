"""LangGraph wiring: entry node (any unprocessed new jobs?) -> job agent -> END."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from pulse import store
from pulse.agents import job_agent
from pulse.config import settings
from pulse.state import PulseState
from pulse.tools.linkedin import LinkedInError, search_jobs


def entry(state: PulseState) -> dict:
    """Search every configured query and keep jobs not seen before."""
    cfg = settings["search"]
    seen = store.load()
    jobs = {}
    for query in cfg["queries"]:
        try:
            found, warnings = search_jobs(query, cfg["location"], cfg["posted_within_hours"], cfg["max_jobs"])
        except LinkedInError as e:
            print(f"[entry] search {query!r} failed: {e}")
            continue
        for w in warnings:
            print(f"[entry] {query!r}: {w}")
        for job in found:
            if job.id not in seen:
                jobs.setdefault(job.id, job)
    print(f"[entry] {len(jobs)} new jobs")
    return {"jobs": list(jobs.values())}


def route(state: PulseState) -> str:
    return "job_agent" if state.get("jobs") else END


def build_graph():
    g = StateGraph(PulseState)
    g.add_node("entry", entry)
    g.add_node("job_agent", job_agent.run)
    g.add_edge(START, "entry")
    g.add_conditional_edges("entry", route, ["job_agent", END])
    g.add_edge("job_agent", END)
    return g.compile()
