"""Entry point: Python-driven heartbeat that runs the graph every config.heartbeat_minutes."""

import argparse
import time
import traceback
from datetime import datetime

from pulse.config import settings
from pulse.graph import build_graph


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run a single heartbeat and exit")
    args = parser.parse_args()

    graph = build_graph()
    while True:
        print(f"=== heartbeat {datetime.now():%Y-%m-%d %H:%M:%S}")
        try:
            out = graph.invoke({})
            for r in out.get("results", []):
                score = f" {r['score']}" if "score" in r else ""
                err = f" ({r['error']})" if r.get("error") else ""
                print(f"[{r['status']}{score}] {r['title']} @ {r['company']} {r['url']}{err}")
        except Exception:
            traceback.print_exc()  # keep the heartbeat alive
        if args.once:
            break
        time.sleep(settings["heartbeat_minutes"] * 60)


if __name__ == "__main__":
    main()
