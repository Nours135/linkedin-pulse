"""LangGraph wiring: entry node (any unprocessed new jobs?) -> job agent -> END.
Invoked once per candidate with {"candidate": Candidate}."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from pulse import store
from pulse.agents import job_agent
from pulse.config import settings
from pulse.state import PulseState
from pulse.tools import matching
from pulse.tools.linkedin import LinkedInError, search_jobs


def entry(state: PulseState) -> dict:
    """Search every query in every location and keep jobs this candidate has not seen before
    (or errored and retryable). Queries come from the candidate's queries, else config
    search.queries, else the keywords generated from the candidate's resume."""
    cand = state["candidate"]
    cfg = settings["search"]
    queries = cand.queries or cfg.get("queries") or matching.generate_keywords(cand).search_queries
    locations = cfg.get("locations") or [cfg.get("location", "United States")]
    print(f"[entry {cand.name}] queries: {', '.join(queries)} | locations: {', '.join(locations)}")
    seen = store.load(cand)
    known = frozenset(seen)
    jobs = {}
    for query in queries:
        for location in locations:
            try:
                found, warnings = search_jobs(query, location, cfg["posted_within_hours"],
                                              cfg["max_jobs"], known_ids=known)
            except LinkedInError as e:
                print(f"[entry {cand.name}] search {query!r} in {location} failed: {e}")
                continue
            for w in warnings:
                print(f"[entry {cand.name}] {query!r} in {location}: {w}")
            if len(found) >= cfg["max_jobs"]:
                print(f"[entry {cand.name}] {query!r} in {location}: hit max_jobs={cfg['max_jobs']}, "
                      f"older jobs in the window were not searched")
            for job in found:
                if not store.skip(seen.get(job.id)):
                    jobs.setdefault(job.id, job)
    print(f"[entry {cand.name}] {len(jobs)} new jobs")
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
