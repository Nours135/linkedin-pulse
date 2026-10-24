# linkedin-pulse

```
main.py                    # heartbeat loop
config.yaml                # polling interval, search location, match threshold
.env.example               # API key / SMTP
assets/
  resume.json              # user's JSON resume (sample: John Wang)
  resume.tex.j2            # LaTeX resume template
work/                      # run outputs (gitignored)
pulse/
  config.py                # config, LLM client
  state.py                 # LangGraph State
  store.py                 # dedup of processed jobs
  graph.py                 # entry node → job agent
  agents/
    job_agent.py           # job polling agent
    resume_agent.py        # resume assembly agent
  tools/
    linkedin.py            # LinkedIn search
    linkedin_results/      # saved searches + JDs for manual review (gitignored)
    matching.py            # keywords / pre-filter / match score
    latex.py               # LaTeX render + compile
    email.py               # email generation and sending
```
