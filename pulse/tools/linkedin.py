"""Tool: search LinkedIn jobs and fetch JDs via the public guest API.

Every search and detail fetch is saved under pulse/tools/linkedin_results/ for manual review:
  searches/<timestamp>_<keywords>.json   one file per search run
  jobs/<job_id>.json                     one file per job, with the full description
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
PAGE_DELAY = 1.0  # pause between pages to lower the chance of rate limiting
MAX_START = 1000  # the guest API answers HTTP 400 from this offset on, so one search sees at most 1000 jobs

RESULTS_DIR = Path(__file__).parent / "linkedin_results"
SEARCHES_DIR = RESULTS_DIR / "searches"
JOBS_DIR = RESULTS_DIR / "jobs"


class LinkedInError(Exception):
    """Request to LinkedIn failed or returned something we could not parse."""


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    posted_at: str  # YYYY-MM-DD
    url: str
    description: str = ""  # filled only by fetch_job_detail
    found_at: str = ""     # when the search returned it (ISO, seconds)
    fetched_at: str = ""   # when fetch_job_detail fetched the JD (ISO, seconds)


def _get(url: str, params: dict | None = None, retries: int = 3) -> requests.Response:
    """GET with simple backoff; retries on network errors, 429 and 5xx. Raises LinkedInError."""
    last_error = ""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            last_error = f"network error: {type(e).__name__}: {e}"
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                last_error = "HTTP 429 (rate limited by LinkedIn)"
            elif resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} (LinkedIn server error)"
            else:
                # 4xx other than 429 will not get better on retry
                raise LinkedInError(f"HTTP {resp.status_code} from {url}")
        if attempt < retries - 1:
            time.sleep(2 ** (attempt + 1))
    raise LinkedInError(f"{last_error}, gave up after {retries} attempts")


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _write_json(path: Path, data) -> str | None:
    """Write data as JSON; returns an error message instead of raising, so saving never breaks a search."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except OSError as e:
        return f"could not save {path}: {e}"
    return None


def _parse_card(li) -> Job | None:
    card = li.select_one("[data-entity-urn]")
    if not card:
        return None
    job_id = card["data-entity-urn"].rsplit(":", 1)[-1]
    link = li.select_one("a.base-card__full-link")
    time_tag = li.select_one("time")
    return Job(
        id=job_id,
        title=_text(li.select_one(".base-search-card__title")),
        company=_text(li.select_one(".base-search-card__subtitle")),
        location=_text(li.select_one(".job-search-card__location")),
        posted_at=time_tag.get("datetime", "") if time_tag else "",
        # strip tracking params to get a stable URL
        url=link["href"].split("?", 1)[0] if link else f"https://www.linkedin.com/jobs/view/{job_id}",
    )


def search_jobs(
    keywords: str,
    location: str = "United States",
    posted_within_hours: int = 24,
    max_jobs: int = 25,
    known_ids: set[str] | frozenset[str] = frozenset(),
    save: bool = True,
) -> tuple[list[Job], list[str]]:
    """Search jobs for `keywords` posted in the last `posted_within_hours`, newest first.

    Paging stops at `max_jobs`, or after a page whose cards are all in `known_ids`: results are
    newest first, so everything below that was seen on an earlier run. A single known card does
    not stop it, since promoted jobs can show up out of date order.

    Returns (jobs, warnings). Raises LinkedInError if the first page fails; a failure on a
    later page stops paging and is reported as a warning alongside the partial results.
    Note: the guest API ignores the f_E (experience) and f_WT (remote) filters, so they are not sent.
    """
    if not keywords.strip():
        raise ValueError("keywords must not be empty")
    if posted_within_hours <= 0 or max_jobs <= 0:
        raise ValueError("posted_within_hours and max_jobs must be positive")

    params = {
        "keywords": keywords,
        "location": location,
        "f_TPR": f"r{posted_within_hours * 3600}",
        "sortBy": "DD",  # date descending
        "start": 0,
    }
    jobs: dict[str, Job] = {}
    warnings: list[str] = []
    bad_cards = 0
    now = datetime.now()
    while len(jobs) < max_jobs and params["start"] < MAX_START:
        try:
            resp = _get(SEARCH_URL, params)
        except LinkedInError as e:
            if params["start"] == 0:
                raise
            warnings.append(f"stopped paging at offset {params['start']}: {e}")
            break
        if not resp.text.strip():
            break
        cards = BeautifulSoup(resp.text, "html.parser").find_all("li")
        if not cards:
            break
        page_ids = []
        for li in cards:
            try:
                job = _parse_card(li)
            except (KeyError, AttributeError, TypeError):
                bad_cards += 1
                continue
            if not job:
                continue
            page_ids.append(job.id)
            if job.id not in jobs:
                job.found_at = now.isoformat(timespec="seconds")
                jobs[job.id] = job
        params["start"] += len(cards)
        if page_ids and known_ids.issuperset(page_ids):
            break  # caught up with an earlier run
        time.sleep(PAGE_DELAY)

    if bad_cards:
        warnings.append(f"skipped {bad_cards} job cards that could not be parsed (page layout may have changed)")

    # promoted jobs can break the ordering, so sort by date again
    result = sorted(jobs.values(), key=lambda j: j.posted_at, reverse=True)[:max_jobs]

    if save:
        slug = re.sub(r"[^a-z0-9]+", "-", keywords.lower()).strip("-") or "all"
        err = _write_json(SEARCHES_DIR / f"{now:%Y%m%d-%H%M%S}_{slug}.json", {
            "keywords": keywords,
            "location": location,
            "posted_within_hours": posted_within_hours,
            "fetched_at": now.isoformat(timespec="seconds"),
            "count": len(result),
            "warnings": warnings,
            "jobs": [asdict(j) for j in result],
        })
        if err:
            warnings.append(err)
    return result, warnings


def fetch_job_detail(job: Job, save: bool = True) -> Job:
    """Fill in the JD body and criteria (Seniority level / Employment type, etc.).

    Raises LinkedInError if the request fails or no description is found.
    """
    if not job.id.strip().isdigit():
        raise ValueError(f"job id must be numeric, got {job.id!r}")
    resp = _get(DETAIL_URL.format(job_id=job.id))
    soup = BeautifulSoup(resp.text, "html.parser")
    desc = soup.select_one(".description__text")
    if desc:
        # drop the "Show more / Show less" button text
        for btn in desc.select("button"):
            btn.decompose()
        body = desc.get_text("\n", strip=True)
    else:
        raise LinkedInError(f"no description found for job {job.id} (removed posting or page layout changed)")
    criteria = [
        f"{_text(item.select_one('.description__job-criteria-subheader'))}: "
        f"{_text(item.select_one('.description__job-criteria-text'))}"
        for item in soup.select(".description__job-criteria-item")
    ]
    job.description = "\n".join(filter(None, [body, *criteria]))
    job.description = re.sub(r"\n{3,}", "\n\n", job.description)
    job.fetched_at = datetime.now().isoformat(timespec="seconds")

    if save:
        _write_json(JOBS_DIR / f"{job.id}.json", asdict(job))  # a save failure should not lose the JD
    return job


# ---- LangChain tools for the agent ----
# Tools always return plain text and never raise: errors come back as "ERROR: ..." so the
# agent can read them and decide whether to retry, change arguments or skip.

@tool
def linkedin_search(keywords: str, location: str = "United States",
                    posted_within_hours: int = 24, max_jobs: int = 25) -> str:
    """Search the newest LinkedIn jobs for `keywords`, newest first.
    Returns one line per job: [job_id] title | company | location | posted date | url.
    Use linkedin_job_detail with a job_id to get the full description."""
    try:
        jobs, warnings = search_jobs(keywords, location, posted_within_hours, max_jobs)
    except ValueError as e:
        return f"ERROR: invalid arguments: {e}"
    except LinkedInError as e:
        return f"ERROR: LinkedIn search failed: {e}. Wait a while before retrying, or try again next heartbeat."
    except Exception as e:  # last resort so the agent loop never crashes on this tool
        return f"ERROR: unexpected failure in linkedin_search: {type(e).__name__}: {e}"

    scope = f'"{keywords}" in {location} (last {posted_within_hours}h)'
    if jobs:
        lines = [f"Found {len(jobs)} jobs for {scope}, newest first:"]
        lines += [
            f"{n}. [{j.id}] {j.title} | {j.company} | {j.location} | {j.posted_at} | {j.url}"
            for n, j in enumerate(jobs, 1)
        ]
    else:
        lines = [f"No jobs found for {scope}. Try broader keywords or a longer time window."]
    lines += [f"WARNING: {w}" for w in warnings]
    return "\n".join(lines)


@tool
def linkedin_job_detail(job_id: str) -> str:
    """Fetch the full job description text for a LinkedIn job id (the number in [brackets])."""
    job = Job(id=job_id.strip().strip("[]"), title="", company="", location="", posted_at="", url="")
    try:
        return fetch_job_detail(job).description
    except ValueError as e:
        return f"ERROR: invalid arguments: {e}"
    except LinkedInError as e:
        return f"ERROR: could not fetch job {job.id}: {e}"
    except Exception as e:
        return f"ERROR: unexpected failure in linkedin_job_detail: {type(e).__name__}: {e}"
