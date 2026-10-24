# TODO

## Pipeline gaps

- [x] **Job agent -> resume agent wiring.** `job_agent._build_resume` called a non-existent
  `resume_agent.run_resume_agent`; the error was swallowed, so every email went out without a
  resume. Now calls `resume_agent.tailor(job.id, job.description, match)`.
- [ ] **Per-component bullet count.** The resume agent only chooses which components appear and
  how many; bullets per component are fixed in `components/<key>.tex`. To let strong matches get
  more bullets and weak ones fewer:
  - give each component more candidate bullets than it shows by default
  - extend the selection from `{section: [key]}` to `{key: [bullet_idx]}`
  - render only the chosen bullets in `render_resume`
  - let the fit loop add/drop bullets, not just whole components
- [x] **matching bypasses the LLM middleware.** Keyword and score calls now go through
  `llm.complete` (`matching._parse` builds the JSON schema and validates the reply; the middleware
  only passes `output_config` through), so they hit `work/llm_cache/` and log to
  `llm_usage.jsonl` under tags `match.keywords` / `match.score`.
- [ ] **Persist generated keywords.** The response cache already keeps them stable across
  restarts until the resume text changes, but they are not in a readable file; write them under
  `work/` so they can be inspected and hand-edited.
- [ ] **Keyword pre-filter is loose.** 3 hits out of 25-40 keywords, matched against the whole JD
  (benefits text included). Consider must-have vs nice-to-have weights and synonyms
  (go/golang, k8s/kubernetes, js/javascript).
- [ ] **Search queries are hard-coded.** `config.yaml` `search.queries` is fixed; optionally
  generate them from the resume like the keywords.

## Cleanup

- [x] Remove the server-side fallback header/body in `matching._ask` (dead with Haiku).
- [ ] Delete `temp_test_fit.py` once the fit loop is settled.
