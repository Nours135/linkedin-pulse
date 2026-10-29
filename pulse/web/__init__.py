"""Local web UI: a table of every job the pipeline recorded, with your own status and notes.

    python -m pulse.web [--host 127.0.0.1] [--port 8765]

Reads work/<candidate>/jobs.json (read-only) and writes work/<candidate>/tracking.json.
There is no auth, so it binds to localhost by default.
"""
