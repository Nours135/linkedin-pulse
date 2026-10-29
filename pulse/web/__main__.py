"""HTTP server for the web UI, stdlib only. See pulse/web/__init__.py for usage.

Routes:
  GET  /                                  the page (index.html)
  GET  /api/candidates                    candidate names and the allowed tracking statuses
  GET  /api/jobs?candidate=NAME           one row per recorded job, merged with its tracking entry
  GET  /api/job?candidate=NAME&id=ID      the full record, including the job description
  POST /api/track                         {candidate, job_id, status?, notes?} -> tracking entry
  GET  /resume?candidate=NAME&id=ID       the tailored resume PDF
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pulse import candidates, store, tracking
from pulse.tools import matching

INDEX = Path(__file__).with_name("index.html")

# fields the table needs; the description and fit-loop details stay out of the list payload
ROW_FIELDS = ("id", "title", "company", "location", "posted_at", "url", "stage", "status",
              "reason", "score", "recorded_at", "error")


def _tags(rec: dict) -> list[str]:
    """Tags are derived from the stored match; records scored before tagging existed have none."""
    try:
        return matching.Match.model_validate(rec["match"]).tags()
    except Exception:
        return []


def _row(rec: dict, track: dict) -> dict:
    row = {k: rec.get(k) for k in ROW_FIELDS}
    row["tags"] = _tags(rec) if "match" in rec else []
    row["has_resume"] = bool(rec.get("resume_pdf"))
    row["track_status"] = track.get("status", "new")
    row["notes"] = track.get("notes", "")
    row["track_updated_at"] = track.get("updated_at")
    return row


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/candidates":
                self._json({"candidates": [c.name for c in candidates.load()],
                            "statuses": list(tracking.STATUSES)})
            elif url.path == "/api/jobs":
                cand = candidates.get(q.get("candidate", ""))
                tracks = tracking.load(cand)
                self._json([_row(rec, tracks.get(jid, {})) for jid, rec in store.load(cand).items()])
            elif url.path == "/api/job":
                cand = candidates.get(q.get("candidate", ""))
                rec = store.load(cand).get(q.get("id", ""))
                if rec is None:
                    return self._error(404, "unknown job")
                self._json({**rec, "tags": _tags(rec) if "match" in rec else []})
            elif url.path == "/resume":
                cand = candidates.get(q.get("candidate", ""))
                rec = store.load(cand).get(q.get("id", "")) or {}
                pdf = Path(rec.get("resume_pdf") or "").resolve()
                # only ever serve files from this candidate's own work dir
                if not rec.get("resume_pdf") or not pdf.is_relative_to(cand.work_dir.resolve()) or not pdf.is_file():
                    return self._error(404, "no resume for this job")
                self._send(200, pdf.read_bytes(), "application/pdf")
            else:
                self._error(404, "not found")
        except ValueError as e:  # unknown candidate
            self._error(400, str(e))

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/track":
            return self._error(404, "not found")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            cand = candidates.get(body.get("candidate", ""))
            job_id = str(body.get("job_id", ""))
            if job_id not in store.load(cand):
                return self._error(404, "unknown job")
            self._json(tracking.update(cand, job_id, body.get("status"), body.get("notes")))
        except (ValueError, json.JSONDecodeError) as e:
            self._error(400, str(e))

    def log_message(self, fmt, *args) -> None:
        pass  # keep the console quiet; errors still surface as tracebacks


def main() -> None:
    parser = argparse.ArgumentParser(description="Web UI for recorded jobs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    candidates.load()  # fail fast on a broken config
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"linkedin-pulse web UI on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
