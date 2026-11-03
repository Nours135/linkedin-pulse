"""Employer H-1B sponsorship for CS roles, from DOL Labor Condition Application (LCA) data.

Every H-1B petition needs a certified LCA first, and each LCA names the employer, job title and
SOC occupation code, so it shows which companies sponsor computer and data roles specifically.

dol.gov blocks scripted downloads: get the LCA_Disclosure_Data_FY*.xlsx files in a browser from
https://www.dol.gov/agencies/eta/foreign-labor/performance into work/h1b/lca/, then run

  python -m pulse.h1b build               aggregate them into work/h1b/employers.json
  python -m pulse.h1b lookup Google Ramp  CS sponsorship of these companies
  python -m pulse.h1b top [-n 50]         employers with the most CS LCAs

Past fiscal years come as one file per quarter; the current year's file is cumulative year-to-date.
build counts each case number once, so overlapping files do no harm, and keeps the last `h1b.years`
fiscal years found. The job agent tags every job by its company's CS LCAs in that window:
NoH1B (none), H1BWeak (fewer than h1b.tiers[0]), H1B, or H1BStrong (h1b.tiers[1] or more);
without employers.json, jobs get no H-1B tag.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from tqdm import tqdm

from pulse.config import settings, work_dir

DATA_DIR = work_dir / "h1b"
INDEX_PATH = DATA_DIR / "employers.json"
YEARS = settings.get("h1b", {}).get("years", 2)
# CS LCAs in the window at which a company counts as H1B and as H1BStrong; fewer than the first is H1BWeak
TIERS = settings.get("h1b", {}).get("tiers", [3, 20])

# "Certified - Withdrawn": certified, then withdrawn by the employer, who still meant to sponsor
CERTIFIED = {"Certified", "Certified - Withdrawn"}

# trailing words that only state the legal form, so "Google" finds "GOOGLE LLC"
SUFFIXES = {"INC", "INCORPORATED", "LLC", "L", "C", "LLP", "LP", "PLLC", "PC", "LTD", "LIMITED",
            "CORP", "CORPORATION", "CO", "COMPANY", "PLC", "NA", "N", "A", "THE", "USA", "US", "U", "S",
            "AND"}  # "AND" is left over from "& Co."


def normalize(name: str) -> str:
    """'Google, LLC' and 'GOOGLE LLC' -> 'GOOGLE'. A 'd/b/a' trade name and parentheses are dropped."""
    s = re.split(r"\bD\s*/?\s*B\s*/?\s*A\b", name.upper())[0].replace("&", " AND ")
    s = re.sub(r"\(.*?\)", " ", s)  # "Amazon Web Services (AWS)"
    words = re.sub(r"[^A-Z0-9]+", " ", s).split()
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "THE":
        words.pop(0)
    return " ".join(words)


def is_cs(soc: str) -> bool:
    """SOC 15-1xxx: computer occupations (software developers, research scientists, ...);
    15-2051: data scientists; 17-2061: computer hardware engineers."""
    return soc.startswith(("15-1", "15-2051", "17-2061"))


def _fy(d) -> int:
    """Federal fiscal year of a date; it starts on October 1."""
    if not isinstance(d, (date, datetime)):
        d = date.fromisoformat(str(d)[:10])
    return d.year + (d.month >= 10)


def _read(path: Path):
    """(case_number, fy, employer, soc_code, job_title) for every certified H-1B LCA in one file."""
    from python_calamine import CalamineWorkbook
    rows = CalamineWorkbook.from_path(str(path)).get_sheet_by_index(0).iter_rows()
    col = {str(h).strip().upper(): i for i, h in enumerate(next(rows))}
    case, status, visa, decided = col["CASE_NUMBER"], col["CASE_STATUS"], col["VISA_CLASS"], col["DECISION_DATE"]
    employer, soc, title = col["EMPLOYER_NAME"], col["SOC_CODE"], col["JOB_TITLE"]
    for r in tqdm(rows, desc=path.name, unit=" rows", leave=False):
        if r[visa] == "H-1B" and r[status] in CERTIFIED and r[employer]:
            yield r[case], _fy(r[decided]), str(r[employer]).strip(), str(r[soc]), str(r[title]).strip()


def build(years: int = YEARS) -> dict:
    """Aggregate every work/h1b/lca/*.xlsx into employers.json: per normalized employer name, its most
    common raw name, CS and total certified H-1B LCAs, and its top CS job titles."""
    files = sorted((DATA_DIR / "lca").glob("*.xlsx"))
    if not files:
        raise SystemExit(f"no LCA files in {DATA_DIR / 'lca'}; download LCA_Disclosure_Data_FY*.xlsx "
                         "from https://www.dol.gov/agencies/eta/foreign-labor/performance")
    seen: set[str] = set()
    lcas = []
    for path in files:
        before = len(lcas)
        for case, *rest in _read(path):
            if case not in seen:
                seen.add(case)
                lcas.append(rest)
        print(f"[h1b] {path.name}: {len(lcas) - before} new certified H-1B LCAs")
    hi = max(fy for fy, *_ in lcas)
    lo = hi - years + 1

    names: dict[str, Counter] = defaultdict(Counter)
    titles: dict[str, Counter] = defaultdict(Counter)
    cs: Counter = Counter()
    total: Counter = Counter()
    for fy, employer, soc, title in lcas:
        if fy < lo:
            continue
        norm = normalize(employer)
        names[norm][employer] += 1
        total[norm] += 1
        if is_cs(soc):
            cs[norm] += 1
            titles[norm][title.title()] += 1
    index = {
        "fy": [lo, hi],
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "files": [p.name for p in files],
        "employers": {n: {"name": names[n].most_common(1)[0][0], "cs": cs[n], "total": total[n],
                          "titles": [t for t, _ in titles[n].most_common(3)]} for n in sorted(total)},
    }
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False))
    print(f"[h1b] FY{lo}-FY{hi}: {sum(total.values())} LCAs, {sum(cs.values())} CS, "
          f"{len(total)} employers ({sum(1 for n in cs if cs[n])} with CS) -> {INDEX_PATH}")
    return index


@lru_cache(maxsize=1)
def _load(mtime: float) -> tuple[dict, list[str]]:
    # keyed by mtime, so a rebuild is picked up without restarting the heartbeat or the web UI
    index = json.loads(INDEX_PATH.read_text())
    return index, list(index["employers"])  # keys are written sorted, for the prefix search


def _index() -> tuple[dict, list[str]] | None:
    return _load(INDEX_PATH.stat().st_mtime) if INDEX_PATH.exists() else None


def _match(keys: list[str], norm: str) -> tuple[list[str], str | None]:
    """Employer keys that count as this company, and how they were found. The name itself plus every
    employer whose name starts with its words count together, so "Amazon" sums AMAZON, AMAZON COM
    SERVICES, AMAZON WEB SERVICES, ... (a tiny exact "AMAZON LLC" must not hide the big ones). With no
    hit, trailing words are dropped ("Google DeepMind" -> GOOGLE). Backing off may hit another company
    with the same first word, which only keeps a job NoH1B would have dropped; the matched names show
    what was counted."""
    words = norm.split()
    for n in range(len(words), 0, -1):
        q = " ".join(words[:n])
        if n < len(words) and len(q) < 4:
            break  # "AI", "THE": too short to mean one company
        i = bisect.bisect_left(keys, q)
        hits = [q] if i < len(keys) and keys[i] == q else []
        j = bisect.bisect_left(keys, q + " ")
        while j < len(keys) and keys[j].startswith(q + " "):
            hits.append(keys[j])
            j += 1
        if hits:
            how = "backoff" if n < len(words) else "exact" if hits == [q] else "prefix"
            return hits, how
    return [], None


def lookup(company: str) -> dict | None:
    """CS H-1B sponsorship of a company in the index's fiscal years, or None without employers.json."""
    loaded = _index()
    if loaded is None:
        return None
    index, keys = loaded
    hits, how = _match(keys, normalize(company))
    rows = sorted((index["employers"][k] for k in hits), key=lambda e: (-e["cs"], -e["total"]))
    titles = Counter(t for e in rows for t in e["titles"])
    return {"company": company, "fy": index["fy"], "match": how, "matched": [e["name"] for e in rows[:5]],
            "cs": sum(e["cs"] for e in rows), "total": sum(e["total"] for e in rows),
            "titles": [t for t, _ in titles.most_common(3)]}


def tags(info: dict | None) -> list[str]:
    """The company's sponsorship tier for CS roles, by its certified H-1B LCAs: ["NoH1B"] for none,
    then ["H1BWeak"], ["H1B"] and ["H1BStrong"] at the TIERS cutoffs; [] without an index."""
    if info is None:
        return []
    cs = info.get("cs", 0)
    if not cs:
        return ["NoH1B"]
    return ["H1BWeak"] if cs < TIERS[0] else ["H1B"] if cs < TIERS[1] else ["H1BStrong"]


def summary(info: dict | None) -> str:
    """One line for the email body and the web UI."""
    if info is None:
        return ""
    lo, hi = info["fy"]
    if not info["matched"]:
        return f"H-1B: no LCA for {info['company']!r} in FY{lo}-FY{hi}"
    titles = f" ({', '.join(info['titles'])})" if info["titles"] else ""
    return (f"H-1B: {info['cs']} CS / {info['total']} certified LCAs in FY{lo}-FY{hi}{titles}; "
            f"{info['match']} match on {', '.join(info['matched'][:3])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Employer H-1B sponsorship for CS roles from DOL LCA data")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="aggregate work/h1b/lca/*.xlsx into employers.json")
    b.add_argument("--years", type=int, default=YEARS, help=f"fiscal years counted (default {YEARS})")
    lk = sub.add_parser("lookup", help="CS sponsorship of one or more companies")
    lk.add_argument("company", nargs="+")
    tp = sub.add_parser("top", help="employers with the most CS LCAs")
    tp.add_argument("-n", type=int, default=50)
    args = parser.parse_args()

    if args.cmd == "build":
        build(args.years)
        return
    loaded = _index()
    if loaded is None:
        raise SystemExit(f"no {INDEX_PATH}; run `python -m pulse.h1b build` first")
    if args.cmd == "lookup":
        for c in args.company:
            info = lookup(c)
            print(f"{c}: {' '.join(tags(info))}\n  {summary(info)}")
    else:
        index, _ = loaded
        lo, hi = index["fy"]
        print(f"FY{lo}-FY{hi}\n{'employer':50} {'CS':>7} {'all':>7}")
        for e in sorted(index["employers"].values(), key=lambda e: -e["cs"])[:args.n]:
            print(f"{e['name'][:50]:50} {e['cs']:>7} {e['total']:>7}")


if __name__ == "__main__":
    main()
