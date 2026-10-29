"""Entry point: Python-driven heartbeat that runs the graph for every candidate every
config.heartbeat_minutes."""

import argparse
import os
import shutil
import time
import traceback
from datetime import datetime

from pulse import candidates
from pulse.config import settings
from pulse.graph import build_graph
from pulse.tools import email

TEXBIN = "/Library/TeX/texbin"  # where MacTeX/BasicTeX install pdflatex


def check_pdflatex() -> None:
    """Without pdflatex every resume fails, yet the job is still recorded as sent and never
    retried, so refuse to start. Shells opened before installing TeX lack TEXBIN on PATH."""
    if not shutil.which("pdflatex") and os.path.isdir(TEXBIN):
        os.environ["PATH"] = f"{TEXBIN}{os.pathsep}{os.environ.get('PATH', '')}"
    if not shutil.which("pdflatex"):
        raise SystemExit("pdflatex not found on PATH; install a TeX distribution (e.g. BasicTeX)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run a single heartbeat and exit")
    parser.add_argument("--candidate", action="append", metavar="NAME",
                        help="only run this candidate from config.yaml (repeatable; default: all)")
    args = parser.parse_args()

    cands = candidates.load()
    if args.candidate:
        unknown = set(args.candidate) - {c.name for c in cands}
        if unknown:
            raise SystemExit(f"unknown candidate(s): {', '.join(sorted(unknown))}")
        cands = [c for c in cands if c.name in args.candidate]

    # fail fast instead of erroring (or silently losing resumes) on every job
    check_pdflatex()
    email.check_config()
    graph = build_graph()
    while True:
        print(f"=== heartbeat {datetime.now():%Y-%m-%d %H:%M:%S}")
        for cand in cands:
            try:
                out = graph.invoke({"candidate": cand})
                for r in out.get("results", []):
                    score = f" {r['score']}" if "score" in r else ""
                    score += "".join(f" {t}" for t in r.get("tags", []))
                    err = f" ({r['error']})" if r.get("error") else ""
                    print(f"[{cand.name} {r['status']}{score}] {r['title']} @ {r['company']} {r['url']}{err}")
            except Exception:
                traceback.print_exc()  # one candidate failing must not stop the others or the heartbeat
        if args.once:
            break
        time.sleep(settings["heartbeat_minutes"] * 60)


if __name__ == "__main__":
    main()
